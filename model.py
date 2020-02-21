from __future__ import absolute_import, division, print_function

import os
import pdb

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from torchcrf import CRF

class GloveLSTMCRF(nn.Module):
    def __init__(self, config, embedding_path, label_path, pos_path, emb_non_trainable=True, use_crf=False):
        super(GloveLSTMCRF, self).__init__()

        self.config = config
        seq_size = config['n_ctx']
        pos_emb_dim = config['pos_emb_dim']
        lstm_hidden_dim = config['lstm_hidden_dim']
        lstm_num_layers = config['lstm_num_layers']
        lstm_dropout = config['lstm_dropout']
        self.use_crf = use_crf

        # glove embedding layer
        weights_matrix = self.__load_embedding(embedding_path)
        vocab_dim, token_emb_dim = weights_matrix.size()
        self.embed_token = self.__create_embedding_layer(vocab_dim, token_emb_dim, weights_matrix=weights_matrix, non_trainable=emb_non_trainable)

        # pos embedding layer
        self.poss = self.__load_dict(pos_path)
        self.pos_size = len(self.poss)
        self.embed_pos = self.__create_embedding_layer(self.pos_size, pos_emb_dim, weights_matrix=None, non_trainable=False)

        # BiLSTM layer
        emb_dim = token_emb_dim + pos_emb_dim
        self.lstm = nn.LSTM(input_size=emb_dim,
                            hidden_size=lstm_hidden_dim,
                            num_layers=lstm_num_layers,
                            dropout=lstm_dropout,
                            bidirectional=True,
                            batch_first=True)

        self.dropout = nn.Dropout(config['dropout'])

        # projection layer
        self.labels = self.__load_dict(label_path)
        self.label_size = len(self.labels)
        self.linear = nn.Linear(lstm_hidden_dim*2, self.label_size)

        # CRF layer
        if self.use_crf:
            self.crf = CRF(num_tags=self.label_size, batch_first=True)

    def __load_embedding(self, input_path):
        weights_matrix = np.load(input_path)
        weights_matrix = torch.tensor(weights_matrix)
        return weights_matrix

    def __create_embedding_layer(self, vocab_dim, emb_dim, weights_matrix=None, non_trainable=True):
        emb_layer = nn.Embedding(vocab_dim, emb_dim)
        if torch.is_tensor(weights_matrix):
            emb_layer.load_state_dict({'weight': weights_matrix})
        if non_trainable:
            emb_layer.weight.requires_grad = False
        return emb_layer

    def __load_dict(self, input_path):
        dic = {}
        with open(input_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                toks = line.strip().split()
                _key = toks[0]
                _id = int(toks[1])
                dic[_id] = _key
        return dic

    def forward(self, x, tags=None):
        # x : [batch_size, seq_size]
        # tags : [batch_size, seq_size]
        token_ids = x[0]
        pos_ids = x[1]

        # 1. Embedding
        token_embed_out = self.embed_token(token_ids)
        # token_embed_out : [batch_size, seq_size, token_emb_dim]
        pos_embed_out = self.embed_pos(pos_ids)
        # pos_embed_out : [batch_size, seq_size, pos_emb_dim]
        embed_out = torch.cat([token_embed_out, pos_embed_out], dim=-1)
        # embed_out : [batch_size, seq_size, emb_dim=token_emb_dim+pos_emb_dim]
        embed_out = self.dropout(embed_out)

        # 2. LSTM
        lstm_out, (h_n, c_n) = self.lstm(embed_out)
        # lstm_out : [batch_size, seq_size, lstm_hidden_dim*2]
        lstm_out = self.dropout(lstm_out)

        # 3. Output
        logits = self.linear(lstm_out)
        # logits : [batch_size, seq_size, label_size]
        if not self.use_crf: return logits
        if tags is not None: # given golden ys(answer)
            device = self.config['device']
            mask = torch.sign(torch.abs(token_ids)).to(torch.uint8).to(device)
            # mask : [batch_size, seq_size]
            log_likelihood = self.crf(logits, tags, mask=mask, reduction='mean')
            prediction = self.crf.decode(logits, mask=mask)
            # prediction : [batch_size, seq_size]
            return logits, log_likelihood, prediction
        else:
            prediction = self.crf.decode(logits)
            return logits, prediction

class BertLSTMCRF(nn.Module):
    def __init__(self, config, bert_config, bert_model, label_path, pos_path, use_crf=False, use_pos=False, disable_lstm=False, feature_based=False):
        super(BertLSTMCRF, self).__init__()

        self.config = config
        seq_size = config['n_ctx']
        pos_emb_dim = config['pos_emb_dim']
        lstm_hidden_dim = config['lstm_hidden_dim']
        lstm_num_layers = config['lstm_num_layers']
        lstm_dropout = config['lstm_dropout']
        self.use_crf = use_crf
        self.use_pos = use_pos
        self.disable_lstm = disable_lstm

        # bert embedding
        self.bert_config = bert_config
        self.bert_model = bert_model
        self.feature_based = feature_based

        # pos embedding layer
        self.poss = self.__load_dict(pos_path)
        self.pos_size = len(self.poss)
        self.embed_pos = self.__create_embedding_layer(self.pos_size, pos_emb_dim, weights_matrix=None, non_trainable=False)

        # BiLSTM layer
        if self.use_pos:
            emb_dim = bert_config.hidden_size + pos_emb_dim
        else:
            emb_dim = bert_config.hidden_size
        if not self.disable_lstm:
            self.lstm = nn.LSTM(input_size=emb_dim,
                                hidden_size=lstm_hidden_dim,
                                num_layers=lstm_num_layers,
                                dropout=lstm_dropout,
                                bidirectional=True,
                                batch_first=True)

        self.dropout = nn.Dropout(config['dropout'])

        # projection layer
        self.labels = self.__load_dict(label_path)
        self.label_size = len(self.labels)
        if not self.disable_lstm:
            self.linear = nn.Linear(lstm_hidden_dim*2, self.label_size)
        else:
            self.linear = nn.Linear(emb_dim, self.label_size)

        # CRF layer
        if self.use_crf:
            self.crf = CRF(num_tags=self.label_size, batch_first=True)

    def __load_embedding(self, input_path):
        weights_matrix = np.load(input_path)
        weights_matrix = torch.tensor(weights_matrix)
        return weights_matrix

    def __create_embedding_layer(self, vocab_dim, emb_dim, weights_matrix=None, non_trainable=True):
        emb_layer = nn.Embedding(vocab_dim, emb_dim)
        if torch.is_tensor(weights_matrix):
            emb_layer.load_state_dict({'weight': weights_matrix})
        if non_trainable:
            emb_layer.weight.requires_grad = False
        return emb_layer

    def __load_dict(self, input_path):
        dic = {}
        with open(input_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                toks = line.strip().split()
                _key = toks[0]
                _id = int(toks[1])
                dic[_id] = _key
        return dic

    def __compute_bert_embedding(self, x):
        if self.feature_based:
            # feature-based
            with torch.no_grad():
                bert_outputs = self.bert_model(input_ids=x[0],
                                               attention_mask=x[1],
                                               token_type_ids=x[2])
                embedded = bert_outputs[0]
        else:
            # fine-tuning
            # x[0], x[1], x[2] : [batch_size, seq_size]
            bert_outputs = self.bert_model(input_ids=x[0],
                                           attention_mask=x[1],
                                           token_type_ids=x[2])
            embedded = bert_outputs[0]
            # [batch_size, seq_size, hidden_size]
            # [batch_size, 0, hidden_size] corresponding to [CLS] == 'embedded[:, 0]'
        return embedded

    def forward(self, x, tags=None):
        # x : [batch_size, seq_size]
        # tags : [batch_size, seq_size]

        # 1. Embedding
        bert_embed_out = self.__compute_bert_embedding(x)
        # bert_embed_out : [batch_size, seq_size, bert_config.hidden_size]
        pos_ids = x[3]
        pos_embed_out = self.embed_pos(pos_ids)
        # pos_embed_out : [batch_size, seq_size, pos_emb_dim]
        if self.use_pos:
            embed_out = torch.cat([bert_embed_out, pos_embed_out], dim=-1)
        else:
            embed_out = bert_embed_out
        # embed_out : [batch_size, seq_size, emb_dim]
        embed_out = self.dropout(embed_out)

        # 2. LSTM
        if not self.disable_lstm:
            lstm_out, (h_n, c_n) = self.lstm(embed_out)
            # lstm_out : [batch_size, seq_size, lstm_hidden_dim*2]
            lstm_out = self.dropout(lstm_out)
        else:
            lstm_out = embed_out
            # lstm_out : [batch_size, seq_size, bert_config.hidden_size]

        # 3. Output
        logits = self.linear(lstm_out)
        # logits : [batch_size, seq_size, label_size]
        if not self.use_crf: return logits
        if tags is not None: # given golden ys(answer)
            device = self.config['device']
            input_ids = x[0]
            mask = torch.sign(torch.abs(input_ids)).to(torch.uint8).to(device)
            # mask : [batch_size, seq_size]
            log_likelihood = self.crf(logits, tags, mask=mask, reduction='mean')
            prediction = self.crf.decode(logits, mask=mask)
            # prediction : [batch_size, seq_size]
            return logits, log_likelihood, prediction
        else:
            prediction = self.crf.decode(logits)
            return logits, prediction

class ElmoLSTMCRF(nn.Module):
    def __init__(self, config, elmo_model, embedding_path, label_path, pos_path, emb_non_trainable=True, use_crf=False):
        super(ElmoLSTMCRF, self).__init__()

        self.config = config
        seq_size = config['n_ctx']
        pos_emb_dim = config['pos_emb_dim']
        elmo_emb_dim = config['elmo_emb_dim']
        lstm_hidden_dim = config['lstm_hidden_dim']
        lstm_num_layers = config['lstm_num_layers']
        lstm_dropout = config['lstm_dropout']
        self.use_crf = use_crf

        # elmo embedding
        self.elmo_model = elmo_model

        # glove embedding layer
        weights_matrix = self.__load_embedding(embedding_path)
        vocab_dim, token_emb_dim = weights_matrix.size()
        self.embed_token = self.__create_embedding_layer(vocab_dim, token_emb_dim, weights_matrix=weights_matrix, non_trainable=emb_non_trainable)

        # pos embedding layer
        self.poss = self.__load_dict(pos_path)
        self.pos_size = len(self.poss)
        self.embed_pos = self.__create_embedding_layer(self.pos_size, pos_emb_dim, weights_matrix=None, non_trainable=False)

        # BiLSTM layer
        emb_dim = elmo_emb_dim + token_emb_dim + pos_emb_dim
        self.lstm = nn.LSTM(input_size=emb_dim,
                            hidden_size=lstm_hidden_dim,
                            num_layers=lstm_num_layers,
                            dropout=lstm_dropout,
                            bidirectional=True,
                            batch_first=True)

        self.dropout = nn.Dropout(config['dropout'])

        # projection layer
        self.labels = self.__load_dict(label_path)
        self.label_size = len(self.labels)
        self.linear = nn.Linear(lstm_hidden_dim*2, self.label_size)

        # CRF layer
        if self.use_crf:
            self.crf = CRF(num_tags=self.label_size, batch_first=True)

    def __load_embedding(self, input_path):
        weights_matrix = np.load(input_path)
        weights_matrix = torch.tensor(weights_matrix)
        return weights_matrix

    def __create_embedding_layer(self, vocab_dim, emb_dim, weights_matrix=None, non_trainable=True):
        emb_layer = nn.Embedding(vocab_dim, emb_dim)
        if torch.is_tensor(weights_matrix):
            emb_layer.load_state_dict({'weight': weights_matrix})
        if non_trainable:
            emb_layer.weight.requires_grad = False
        return emb_layer

    def __load_dict(self, input_path):
        dic = {}
        with open(input_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                toks = line.strip().split()
                _key = toks[0]
                _id = int(toks[1])
                dic[_id] = _key
        return dic

    def forward(self, x, tags=None):
        # x : [batch_size, seq_size]
        # tags : [batch_size, seq_size]
        token_ids = x[0]
        pos_ids = x[1]
        char_ids = x[2]

        device = self.config['device']
        mask = torch.sign(torch.abs(token_ids)).to(torch.uint8).to(device)
        # mask : [batch_size, seq_size]

        # 1. Embedding
        elmo_embed_out = self.elmo_model(char_ids)['elmo_representations'][0]
        # elmo_embed_out  : [batch_size, seq_size, elmo_emb_dim]
        '''
        masks = mask.unsqueeze(-1).to(torch.float)
        # masks : [batch_size, seq_size, elmo_emb_dim]
        elmo_embed_out *= masks # auto-braodcasting
        '''
        token_embed_out = self.embed_token(token_ids)
        # token_embed_out : [batch_size, seq_size, token_emb_dim]
        pos_embed_out = self.embed_pos(pos_ids)
        # pos_embed_out   : [batch_size, seq_size, pos_emb_dim]
        embed_out = torch.cat([elmo_embed_out, token_embed_out, pos_embed_out], dim=-1)
        # embed_out : [batch_size, seq_size, emb_dim]
        embed_out = self.dropout(embed_out)

        # 2. LSTM
        lstm_out, (h_n, c_n) = self.lstm(embed_out)
        # lstm_out : [batch_size, seq_size, lstm_hidden_dim*2]
        lstm_out = self.dropout(lstm_out)

        # 3. Output
        logits = self.linear(lstm_out)
        # logits : [batch_size, seq_size, label_size]
        if not self.use_crf: return logits
        if tags is not None: # given golden ys(answer)
            log_likelihood = self.crf(logits, tags, mask=mask, reduction='mean')
            prediction = self.crf.decode(logits, mask=mask)
            # prediction : [batch_size, seq_size]
            return logits, log_likelihood, prediction
        else:
            prediction = self.crf.decode(logits)
            return logits, prediction

