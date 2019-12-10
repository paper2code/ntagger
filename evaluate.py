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
from model import GloveLSTMCRF, BertLSTMCRF
from dataset import CoNLLGloveDataset, CoNLLBertDataset
from torch.utils.data import DataLoader
import numpy as np
from seqeval.metrics import precision_score, recall_score, f1_score

from tqdm import tqdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_config(opt):
    try:
        with open(opt.config, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        config = dict()
    return config

def prepare_dataset(opt, filepath, DatasetClass, shuffle=False, num_workers=1):
    dataset = DatasetClass(filepath)
    sampler = None
    loader = DataLoader(dataset, batch_size=opt.batch_size, \
            shuffle=shuffle, num_workers=num_workers, sampler=sampler)
    logger.info("[{} data loaded]".format(filepath))
    return loader

def to_device(x, device):
    if type(x) != list: # torch.tensor
        x = x.to(device)
    else:               # list of torch.tensor
        for i in range(len(x)):
            x[i] = x[i].to(device)
    return x

def to_numpy(x):
    if type(x) != list: # torch.tensor
        x = x.detach().cpu().numpy()
    else:               # list of torch.tensor
        for i in range(len(x)):
            x[i] = x[i].detach().cpu().numpy()
    return x

def write_prediction(opt, ys, preds, labels, pad_label_id):
    try:
        with open(opt.pred_path, 'w', encoding='utf-8') as f:
            for i in range(ys.shape[0]):     # foreach sentence
                for j in range(ys.shape[1]): # foreach token
                    if ys[i][j] != pad_label_id:
                        pred_label = labels[preds[i][j]]
                        f.write(pred_label + '\n')
                f.write('\n')
    except Exception as e:
        logger.warn(str(e))

def evaluate(opt):
    # set config
    config = load_config(opt)
    config['device'] = opt.device
    config['opt'] = opt

    test_data_path = opt.data_path
    batch_size = opt.batch_size
    device = opt.device
    torch.set_num_threads(opt.num_thread)

    # prepare test dataset
    if opt.emb_class == 'glove':
        test_loader = prepare_dataset(opt, test_data_path, CoNLLGloveDataset, shuffle=False, num_workers=1)
    if opt.emb_class == 'bert':
        test_loader = prepare_dataset(opt, test_data_path, CoNLLBertDataset, shuffle=False, num_workers=1)
 
    # load pytorch model checkpoint
    logger.info("[Loading model...]")
    if device == 'cpu':
        checkpoint = torch.load(opt.model_path, map_location=lambda storage, loc: storage)
    else:
        checkpoint = torch.load(opt.model_path)

    # prepare model and load parameters
    if opt.emb_class == 'glove':
        model = GloveLSTMCRF(config, opt.embedding_path, opt.label_path, emb_non_trainable=True, use_crf=opt.use_crf)
    if opt.emb_class == 'bert':
        from transformers import BertTokenizer, BertConfig, BertModel
        bert_tokenizer = BertTokenizer.from_pretrained(opt.bert_output_dir,
                                                       do_lower_case=opt.bert_do_lower_case)
        bert_model = BertModel.from_pretrained(opt.bert_output_dir)
        bert_config = bert_model.config
        ModelClass = BertLSTMCRF
        model = ModelClass(config, bert_config, bert_model, opt.label_path)
    model.load_state_dict(checkpoint)
    model = model.to(device)
    logger.info("[Loaded]")
 
    # evaluation
    model.eval()
    preds = None
    ys    = None
    n_batches = len(test_loader)
    total_examples = 0
    whole_st_time = time.time()
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
    whole_time = int((time.time()-whole_st_time)*1000)
    avg_time = whole_time / total_examples
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
    write_prediction(opt, ys, preds, labels, pad_label_id)

    logger.info("[F1] : {}, {}".format(f1, total_examples))
    logger.info("[Elapsed Time] : {}ms, {}ms on average".format(whole_time, avg_time))

def main():
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--data_path', type=str, default='data/conll2003/test.txt.ids')
    parser.add_argument('--embedding_path', type=str, default='data/conll2003/embedding.npy')
    parser.add_argument('--label_path', type=str, default='data/conll2003/label.txt')
    parser.add_argument('--pred_path', type=str, default='data/conll2003/pred.txt')
    parser.add_argument('--config', type=str, default='config.json')
    parser.add_argument('--model_path', type=str, default='pytorch-model.pt')
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--num_thread', type=int, default=1)
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--emb_class', type=str, default='glove', help='glove | bert')
    parser.add_argument('--use_crf', action="store_true")
    # for BERT
    parser.add_argument("--bert_do_lower_case", action="store_true",
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--bert_output_dir", type=str, default='bert-checkpoint',
                        help="The output directory where the model predictions and checkpoints will be written.")
    opt = parser.parse_args()

    evaluate(opt) 

if __name__ == '__main__':
    main()