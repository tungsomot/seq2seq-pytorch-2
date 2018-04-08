#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2017 Yifan WANG <yifanwang1993@gmail.com>
#
# Distributed under terms of the MIT license.

"""

"""

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
from torch.utils import data
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from torch.distributions import Categorical
from torchtext.vocab import Vocab
# from torchtext.vocab import GloVe
from torchtext.data import Field, Pipeline, RawField, Dataset, Example, BucketIterator
from torchtext.data import get_tokenizer
import os, time, sys, datetime, argparse, pickle

from model import EncoderRNN, DecoderRNN
import config
from utils import *

EOS = "<eos>"
SOS = "<sos>"
PAD = "<pad>"
np.random.seed(666)


def main(args):
    c = getattr(config, args.config)()
    c['use_cuda'] = args.use_cuda
    logger  = init_logging('log/{0}{1}.log'.format(c['prefix'], time.time()))
    start = time.time()
    logger.info(since(start) + "Loading data with configuration '{0}'...".format(args.config))
    datasets, src_field, trg_field = load_data(c)
    # TODO: validation dataset

    train = datasets['train']
    src_field.build_vocab(train, max_size=c['encoder_vocab'])
    trg_field.build_vocab(train, max_size=c['decoder_vocab'])

    logger.info("Source vocab: {0}".format(len(src_field.vocab.itos)))
    logger.info("Target vocab: {0}".format(len(trg_field.vocab.itos)))

    test = datasets['test']
    n_test = len(test.examples)

    N = len(train.examples)
    batch_per_epoch = N // c['batch_size'] if N % c['batch_size'] == 0 else N // c['batch_size']+1
    n_iters = batch_per_epoch * c['num_epochs']

    logger.info(since(start) + "{0} training samples, {1} epochs, batch size={2}, {3} batches per epoch.".format(N, c['num_epochs'], c['batch_size'], batch_per_epoch))

    train_iter = iter(BucketIterator(
        dataset=train, batch_size=c['batch_size'],
        sort_key=lambda x: -len(x.src), device=-1))

    test_iter = iter(BucketIterator(
        dataset=test, batch_size=1,
        sort_key=lambda x: -len(x.src), device=-1))

    del train
    del test

    PAD_IDX = trg_field.vocab.stoi[PAD] # default=1

    if args.from_scratch or not os.path.isfile(c['model_path'] + c['prefix'] + 'encoder.pkl') \
            or not os.path.isfile(c['model_path'] + c['prefix'] + 'decoder.pkl'):
        # Train from scratch
        encoder = EncoderRNN(vocab_size=len(src_field.vocab), embed_size=c['encoder_embed_size'],\
                hidden_size=c['encoder_hidden_size'], padding_idx=PAD_IDX, n_layers=c['num_layers'])
        decoder = DecoderRNN(vocab_size=len(trg_field.vocab), embed_size=c['decoder_embed_size'],\
                hidden_size=c['decoder_hidden_size'], encoder_hidden=c['encoder_hidden_size'],\
                padding_idx=PAD_IDX, n_layers=c['num_layers'])
        # TODO: save training log
        info = { 'global_step':0,
                'steps':[],
                'loss':[],
                'rl_score': [],
                'score':[]}
    else:
        # Load from saved model
        logger.info(since(start) + "Loading models...")
        encoder = torch.load(c['model_path'] + c['prefix'] + 'encoder.pkl')
        decoder = torch.load(c['model_path'] + c['prefix'] + 'decoder.pkl')

    if c['use_cuda']:
        encoder.cuda()
        decoder.cuda()
    else:
        encoder.cpu()
        decoder.cpu()

    CEL = nn.CrossEntropyLoss(size_average=True, ignore_index=PAD_IDX)
    params = list(encoder.parameters()) +  list(decoder.parameters())
    optimizer = optim.Adam(params, lr=c['learning_rate'])
    print_loss = 0

    logger.info(since(start) + "Start training... {0} iterations...".format(n_iters))

    # Start training
    for e in range(c['num_epochs']):
        for j in range(batch_per_epoch): 
            i = batch_per_epoch * e + j+1

            batch = next(train_iter)
            encoder_inputs, encoder_lengths = batch.src
            decoder_inputs, decoder_lengths = batch.trg
            # GPU
            encoder_inputs = cuda(encoder_inputs, c['use_cuda'])
            decoder_inputs = cuda(decoder_inputs, c['use_cuda'])
            
            encoder_unpacked, encoder_hidden = encoder(encoder_inputs, encoder_lengths, return_packed=False)
            # we don't remove the last symbol
            decoder_unpacked, decoder_hidden = decoder(decoder_inputs[:-1,:], encoder_hidden, encoder_unpacked, encoder_lengths)
            trg_len, batch_size, d = decoder_unpacked.size()
            # remove first symbol <SOS>
            ce_loss = CEL(decoder_unpacked.view(trg_len*batch_size, d), decoder_inputs[1:,:].view(-1))
            print_loss += ce_loss.data

            assert args.self_critical >= 0. and args.self_critical <= 1.
            if args.self_critical > 1e-5:
                sc_loss = cuda(Variable(torch.Tensor([0.])), c['use_cuda'])
                for j in range(batch_size):
                    enc_input = (encoder_inputs[:,j].unsqueeze(1), torch.LongTensor([encoder_lengths[j]]))
                    # use self critical training
                    greedy_out, _ = sample(encoder, decoder, enc_input, trg_field,
                            max_len=30, greedy=True, config=c)
                    greedy_sent = tostr(clean(greedy_out))
                    sample_out, sample_logp = sample(encoder, decoder, enc_input, trg_field,
                            max_len=30, greedy=False, config=c)
                    sample_sent = tostr(clean(sample_out))
                    # Ground truth
                    gt_sent = tostr(clean(itos(decoder_inputs[:,j].cpu().data.numpy(), trg_field)))
                    greedy_score = score(hyps=greedy_sent, refs=gt_sent, metric='rouge')
                    sample_score = score(hyps=sample_sent, refs=gt_sent, metric='rouge')
                    reward = Variable(torch.Tensor([sample_score["rouge-1"]['f'] - greedy_score["rouge-1"]['f']]), requires_grad=False)
                    reward = cuda(reward, c['use_cuda'])
                    sc_loss -= reward*torch.sum(sample_logp)

                if i % c['log_step'] == 0:
                    logger.info("CE: {0}".format(ce_loss))
                    logger.info("SC: {0}".format(sc_loss))
                    logger.info("GT: {0}".format(gt_sent))
                    logger.info("greedy: {0}, {1}".format(greedy_score['rouge-1']['f'], greedy_sent))
                    logger.info("sample: {0}, {1}".format(sample_score['rouge-1']['f'], sample_sent))
                
                loss = (1-args.self_critical) * ce_loss + args.self_critical * sc_loss
            else:
                loss = ce_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # free memory
            del encoder_inputs, decoder_inputs

            if i % c['save_step'] == 0:
                # TODO: save log
                synchronize(c)
                logger.info(since(start) + "Saving models...")
                torch.save(encoder, c['model_path'] + c['prefix'] + 'encoder.pkl')
                torch.save(decoder, c['model_path'] + c['prefix'] + 'decoder.pkl')

            if i % c['log_step'] == 0:
                synchronize(c)
                logger.info(since(start) + 'epoch {0}/{1}, iteration {2}/{3}'.format(e, c['num_epochs'], i, n_iters))
                logger.info("\tTrain loss: {0}".format(print_loss.cpu().numpy().tolist()[0] / c['log_step']))
                print_loss = 0
                random_eval(encoder, decoder, batch, n=1, src_field=src_field, trg_field=trg_field, config=c,
                        greedy=True, logger=logger)

            if i % c['test_step'] == 0:
                test_loss = 0
                test_rouge = 0
                refs = []
                greedys = []
                for j in range(n_test):
                    test_batch = next(test_iter)
                    test_encoder_inputs, test_encoder_lengths = test_batch.src
                    test_decoder_inputs, test_decoder_lengths = test_batch.trg
                    # GPU
                    test_encoder_inputs = cuda(Variable(test_encoder_inputs.data, volatile=True), c['use_cuda'])
                    test_decoder_inputs = cuda(Variable(test_decoder_inputs.data, volatile=True), c['use_cuda'])

                    test_encoder_packed, test_encoder_hidden = encoder(test_encoder_inputs, test_encoder_lengths)
                    test_encoder_unpacked = pad_packed_sequence(test_encoder_packed)[0]
                    # remove last symbol
                    test_decoder_unpacked, test_decoder_hidden = decoder(test_decoder_inputs[:-1,:], test_encoder_hidden, test_encoder_unpacked, test_encoder_lengths)
                    trg_len, batch_size, d = test_decoder_unpacked.size()
                    # remove first symbol <SOS>
                    test_ce_loss = CEL(test_decoder_unpacked.view(trg_len*batch_size, d), test_decoder_inputs[1:,:].view(-1))
                    test_loss += test_ce_loss.data

                    test_enc_input = (test_encoder_inputs[:,0].unsqueeze(1), torch.LongTensor([test_encoder_lengths[0]]))
                    test_greedy_out, _ = sample(encoder, decoder, test_enc_input, trg_field,
                            max_len=30, greedy=True, config=c)
                    test_greedy_sent = tostr(clean(test_greedy_out))

                    test_gt_sent = tostr(clean(itos(test_decoder_inputs[:,0].cpu().data.numpy(), trg_field)))
                    refs.append(test_gt_sent)
                    greedys.append(test_greedy_sent)

                test_rouge = score(hyps=greedys, refs=refs, metric='rouge')['rouge-1']['f']
                        
                synchronize(c)
                logger.info(since(start) + "Test loss: {0}".format(test_loss.cpu().numpy().tolist()[0]/n_test))
                logger.info(since(start) + "Test ROUGE-1_f: {0}\n".format(test_rouge))

                        




if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default=None ,
                        help='model configurations, defined in config.py')
    parser.add_argument('--from_scratch', type=bool, default=False)
    parser.add_argument('--disable_cuda', type=bool, default=False)
    parser.add_argument('--self_critical', type=float, default=0.)
    args = parser.parse_args()
    args.use_cuda = not args.disable_cuda and torch.cuda.is_available()
    if args.use_cuda:
        print("Use GPU...")
    else:
        print("Use CPU...")
    main(args)
