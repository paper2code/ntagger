from __future__ import absolute_import, division, print_function

import sys
import os
import argparse
import json
import time
import pdb
import logging

import torch
import torch.nn as nn
import numpy as np
from seqeval.metrics import precision_score, recall_score, f1_score

from tqdm import tqdm
from util import load_config, to_device, to_numpy
from model import GloveLSTMCRF, GloveDensenetCRF, BertLSTMCRF, ElmoLSTMCRF
from dataset import prepare_dataset, CoNLLGloveDataset, CoNLLBertDataset, CoNLLElmoDataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def write_prediction(opt, ys, preds, labels, pad_label_id, default_label):
    # load test data
    tot_num_line = sum(1 for _ in open(opt.test_path, 'r')) 
    with open(opt.test_path, 'r', encoding='utf-8') as f:
        data = []
        bucket = []
        for idx, line in enumerate(tqdm(f, total=tot_num_line)):
            line = line.strip()
            if line == "":
                data.append(bucket)
                bucket = []
            else:
                entry = line.split()
                assert(len(entry) == 4)
                bucket.append(entry)
        if len(bucket) != 0:
            data.append(bucket)
    # write prediction
    try:
        pred_path = opt.test_path + '.pred'
        with open(pred_path, 'w', encoding='utf-8') as f:
            for i, bucket in enumerate(data):      # foreach sentence
                # from preds
                j_bucket = 0
                for j in range(ys.shape[1]):       # foreach token
                    pred_label = default_label
                    if ys[i][j] != pad_label_id:
                        pred_label = labels[preds[i][j]]
                        entry = bucket[j_bucket]
                        entry.append(pred_label)
                        f.write(' '.join(entry) + '\n')
                        j_bucket += 1
                # remained
                for j, entry in enumerate(bucket): # foreach remained token
                    if j < j_bucket: continue
                    pred_label = default_label
                    entry = bucket[j]
                    entry.append(pred_label)
                    f.write(' '.join(entry) + '\n')
                f.write('\n')
    except Exception as e:
        logger.warn(str(e))

def set_path(config):
    opt = config['opt']
    if config['emb_class'] == 'glove':
        opt.data_path = os.path.join(opt.data_dir, 'test.txt.ids')
    if 'bert' in config['emb_class']:
        opt.data_path = os.path.join(opt.data_dir, 'test.txt.fs')
    if config['emb_class'] == 'elmo':
        opt.data_path = os.path.join(opt.data_dir, 'test.txt.ids')
    opt.embedding_path = os.path.join(opt.data_dir, 'embedding.npy')
    opt.label_path = os.path.join(opt.data_dir, 'label.txt')
    opt.pos_path = os.path.join(opt.data_dir, 'pos.txt')
    opt.test_path = os.path.join(opt.data_dir, 'test.txt')

def prepare_datasets(config):
    opt = config['opt']
    if config['emb_class'] == 'glove':
        DatasetClass = CoNLLGloveDataset
    if 'bert' in config['emb_class']:
        DatasetClass = CoNLLBertDataset
    if config['emb_class'] == 'elmo':
        DatasetClass = CoNLLElmoDataset
    test_loader = prepare_dataset(config, opt.data_path, DatasetClass, shuffle=False, num_workers=1)
    return test_loader

def load_checkpoint(config):
    opt = config['opt']
    if opt.device == 'cpu':
        checkpoint = torch.load(opt.model_path, map_location=lambda storage, loc: storage)
    else:
        checkpoint = torch.load(opt.model_path)
    logger.info("[Loading checkpoint done]")
    return checkpoint

def load_model(config, checkpoint):
    opt = config['opt']
    device = config['device']
    if config['emb_class'] == 'glove':
        if config['enc_class'] == 'bilstm':
            model = GloveLSTMCRF(config, opt.embedding_path, opt.label_path, opt.pos_path,
                                 emb_non_trainable=True, use_crf=opt.use_crf, use_char_cnn=opt.use_char_cnn)
        if config['enc_class'] == 'densenet':
            model = GloveDensenetCRF(config, opt.embedding_path, opt.label_path, opt.pos_path,
                                     emb_non_trainable=True, use_crf=opt.use_crf, use_char_cnn=opt.use_char_cnn)
    if 'bert' in config['emb_class']:
        from transformers import BertTokenizer, BertConfig, BertModel
        from transformers import RobertaConfig, RobertaTokenizer, RobertaModel
        MODEL_CLASSES = {
            "bert": (BertConfig, BertTokenizer, BertModel),
            "roberta": (RobertaConfig, RobertaTokenizer, RobertaModel)
        }
        Config    = MODEL_CLASSES[config['emb_class']][0]
        Tokenizer = MODEL_CLASSES[config['emb_class']][1]
        Model     = MODEL_CLASSES[config['emb_class']][2]
        bert_tokenizer = Tokenizer.from_pretrained(opt.bert_output_dir,
                                                       do_lower_case=opt.bert_do_lower_case)
        bert_model = Model.from_pretrained(opt.bert_output_dir)
        bert_config = bert_model.config
        ModelClass = BertLSTMCRF
        model = ModelClass(config, bert_config, bert_model, bert_tokenizer, opt.label_path, opt.pos_path,
                           use_crf=opt.use_crf, use_pos=opt.bert_use_pos, disable_lstm=opt.bert_disable_lstm)
    if config['emb_class'] == 'elmo':
        from allennlp.modules.elmo import Elmo
        elmo_model = Elmo(opt.elmo_options_file, opt.elmo_weights_file, 2, dropout=0)
        model = ElmoLSTMCRF(config, elmo_model, opt.embedding_path, opt.label_path, opt.pos_path,
                            emb_non_trainable=True, use_crf=opt.use_crf, use_char_cnn=opt.use_char_cnn)
    model.load_state_dict(checkpoint)
    model = model.to(device)
    logger.info("[Loaded]")
    return model

def evaluate(opt):
    # set config
    config = load_config(opt)
    device = torch.device(opt.device)
    torch.set_num_threads(opt.num_thread)
    config['device'] = device
    config['opt'] = opt
    logger.info("%s", config)

    # set path
    set_path(config)

    # prepare test dataset
    test_loader = prepare_datasets(config)
 
    # load pytorch model checkpoint
    checkpoint = load_checkpoint(config)

    # prepare model and load parameters
    model = load_model(config, checkpoint)
 
    # evaluation
    model.eval()
    preds = None
    ys    = None
    n_batches = len(test_loader)
    total_examples = 0
    whole_st_time = time.time()
    first_time = time.time()
    first_examples = 0
    with torch.no_grad():
        for i, (x,y) in enumerate(tqdm(test_loader, total=n_batches)):
            x = to_device(x, device)
            y = to_device(y, device)
            if opt.use_crf:
                logits, prediction = model(x)
            else:
                logits = model(x)
            if preds is None:
                if opt.use_crf:
                    preds = prediction
                else:
                    preds = to_numpy(logits)
                ys = to_numpy(y)
            else:
                if opt.use_crf:
                    preds = np.append(preds, prediction, axis=0)
                else:
                    preds = np.append(preds, to_numpy(logits), axis=0)
                ys = np.append(ys, to_numpy(y), axis=0)
            cur_examples = y.size(0)
            total_examples += cur_examples
            if i == 0: # first one may takes longer time, so ignore in computing duration.
                first_time = int((time.time()-first_time)*1000)
                first_examples = cur_examples
    whole_time = int((time.time()-whole_st_time)*1000)
    avg_time = (whole_time - first_time) / (total_examples - first_examples)
    if not opt.use_crf: preds = np.argmax(preds, axis=2)
    # compute measure using seqeval
    labels = model.labels
    ys_lbs = [[] for _ in range(ys.shape[0])]
    preds_lbs = [[] for _ in range(ys.shape[0])]
    pad_label_id = config['pad_label_id']
    for i in range(ys.shape[0]):     # foreach sentence
        for j in range(ys.shape[1]): # foreach token
            if ys[i][j] != pad_label_id:
                ys_lbs[i].append(labels[ys[i][j]])
                preds_lbs[i].append(labels[preds[i][j]])
    ret = {
        "precision": precision_score(ys_lbs, preds_lbs),
        "recall": recall_score(ys_lbs, preds_lbs),
        "f1": f1_score(ys_lbs, preds_lbs)
    }
    f1 = ret['f1']
    # write predicted labels to file
    default_label = config['default_label']
    write_prediction(opt, ys, preds, labels, pad_label_id, default_label)

    logger.info("[F1] : {}, {}".format(f1, total_examples))
    logger.info("[Elapsed Time] : {}ms, {}ms on average".format(whole_time, avg_time))

def main():
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--config', type=str, default='configs/config-glove.json')
    parser.add_argument('--data_dir', type=str, default='data/conll2003')
    parser.add_argument('--model_path', type=str, default='pytorch-model-glove.pt')
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--num_thread', type=int, default=1)
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--seed', default=5, type=int, help="dummy for BaseModel.")
    parser.add_argument('--use_crf', action='store_true', help="add CRF layer")
    parser.add_argument('--use_char_cnn', action='store_true', help="add Character features")
    # for BERT
    parser.add_argument('--bert_do_lower_case', action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument('--bert_output_dir', type=str, default='bert-checkpoint',
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument('--bert_disable_lstm', action='store_true',
                        help="disable lstm layer")
    parser.add_argument('--bert_use_pos', action='store_true', help="add Part-Of-Speech features")
    # for ELMo
    parser.add_argument('--elmo_options_file', type=str, default='embeddings/elmo_2x4096_512_2048cnn_2xhighway_5.5B_options.json')
    parser.add_argument('--elmo_weights_file', type=str, default='embeddings/elmo_2x4096_512_2048cnn_2xhighway_5.5B_weights.hdf5')

    opt = parser.parse_args()

    evaluate(opt) 

if __name__ == '__main__':
    main()
