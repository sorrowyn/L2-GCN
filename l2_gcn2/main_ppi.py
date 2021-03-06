import torch
import torch.nn as nn
from torch.nn import init
from torch.autograd import Variable

import numpy as np
import time
import random
from sklearn.metrics import f1_score
import os
import yaml
import argparse
import scipy.sparse as sps
import random

import l2_gcn2.utils as utils
import l2_gcn2.net as net


def run(dataset_load_func, seed, dataset, config_file, layer_num, epoch_num):

    # dataset load
    feat_data, labels, Adj, dataset_split = dataset_load_func()
    print('Finished loading dataset.')

    # config parameter load
    with open('./config/' + config_file) as f:
        args = yaml.load(f)
    if not layer_num is None:
        args['layer_num'] = layer_num
    if not epoch_num is None:
        args['layer_train_batch'] = epoch_num
    print(args)

    result_file = './result/' + config_file[:-5] + '_' + str(args['layer_num']) + '_layer_' + str(seed) + '.npy'
    result_loss_data = []

    num_nodes = args['node_num']

    # train, val and test set index
    train = dataset_split['train']
    train.sort()
    val = dataset_split['val']
    test = dataset_split['test']
    test.sort()
    print('trainset size', len(train),
          'valset size', len(test),
          'testset size', len(test))

    # feature and label generate
    feat_train = torch.FloatTensor(feat_data)[train, :]
    label_train = labels[train]
    feat_val = torch.FloatTensor(feat_data)[val, :]
    feat_test = torch.FloatTensor(feat_data)[test, :]

    # Adj matrix generate
    Adj_eye = sps.eye(num_nodes, dtype=np.float32).tocsr()

    Adj_train = Adj[train, :][:, train]
    D_train = Adj_train.sum(axis=0)
    Adj_train = Adj_train.multiply(1/D_train.transpose())
    Adj_train = Adj_train + Adj_eye[train, :][:, train]

    Adj_val = Adj[val, :][:, val]
    D_val = Adj_val.sum(axis=0)
    Adj_val = Adj_val.multiply(1/D_val.transpose())
    Adj_val = Adj_val + Adj_eye[val, :][:, val]

    Adj_test = Adj[test, :][:, test]
    D_test = Adj_test.sum(axis=0)
    Adj_test = Adj_test.multiply(1/D_test.transpose())
    Adj_test = Adj_test + Adj_eye[test, :][:, test]

    print('Finished generating adj matrix.')

    # layerwise training
    times = 0
    epochs = 0
    weight_list = nn.ParameterList()

    loss_func = nn.BCELoss()
    sigmoid = nn.Sigmoid()
    relu = nn.ReLU(inplace=True)

    for l in range(args['layer_num']):

        print('layer ' + str(l+1) + ' training:')

        feat_train = feat_train.to(torch.device('cpu')).numpy()

        start_time = time.time()
        feat_train = Adj_train.dot(feat_train)
        end_time = time.time()
        times = times + ( end_time - start_time )

        feat_train = torch.FloatTensor(feat_train)

        feeder_train = utils.feeder(feat_train, label_train)
        dataset_train = torch.utils.data.DataLoader(dataset=feeder_train, batch_size=args['batch_size'], shuffle=True, drop_last=True)

        if l == 0:
            in_channel = args['feat_dim']
        else:
            in_channel = args['layer_output_dim'][l-1]
        hidden_channel = args['layer_output_dim'][l]
        out_channel = args['class_num']

        net_train = net.net_train(in_channel, hidden_channel, out_channel).to(torch.device(args['device']))
        optimizer = torch.optim.Adam(net_train.parameters(), lr=args['learning_rate'])
        batch = 0
        while True:
            for x, x_label in dataset_train:

                x = x.to(torch.device(args['device']))
                x_label = x_label.to(torch.device(args['device']))

                start_time = time.time()
                optimizer.zero_grad()
                output = net_train(x)
                output = sigmoid(output)
                loss = loss_func(output, x_label)
                loss.backward()
                optimizer.step()
                end_time = time.time()
                times = times + ( end_time - start_time )

            result_loss_data.append(loss.data.cpu().numpy())

            batch = batch + 1
            epochs = epochs + 1
            print('batch', batch, 'loss:', loss.data)
        
            if batch == args['layer_train_batch'][l]:

                w = net_train.get_w()
                w.requires_grad = False

                if l != args['layer_num'] - 1:
                    _w = w.to(torch.device('cpu'))

                    start_time = time.time()
                    feat_train = torch.mm(feat_train, _w)
                    feat_train = relu(feat_train)
                    end_time = time.time()
                    times = times + ( end_time - start_time )

                weight_list.append(w)
                if l == args['layer_num'] - 1:
                    classifier = net_train.get_c()
                    classifier.requires_grad = False
                break

    os.system('nvidia-smi')

    weight_list = weight_list.to(torch.device('cpu'))
    classifier = classifier.to(torch.device('cpu'))

    # np.save(result_file, np.array(result_loss_data))

    # val and test
    net_test = net.net_test()
    with torch.no_grad():
        output_val = net_test(feat_val, Adj_val, weight_list, classifier)
        output_val[output_val>0] = 1
        output_val[output_val<=0] = 0
        output_test = net_test(feat_test, Adj_test, weight_list, classifier)
        output_test[output_test>0] = 1
        output_test[output_test<=0] = 0

    print("accuracy in test:", f1_score(labels[val], output_val.data.numpy(), average="micro"))
    print("accuracy in test:", f1_score(labels[test], output_test.data.numpy(), average="micro"))
    print("average epoch time:", times / epochs)
    print("total time:", times)

    return f1_score(labels[test], output_test.data.numpy(), average="micro"), times


def parser_loader():

    parser = argparse.ArgumentParser(description='L2O_LWGCN')
    parser.add_argument('--config-file', type=str, default='ppi.yaml')
    parser.add_argument('--dataset', type=str, default='ppi')
    parser.add_argument('--layer-num', type=int, default=None)
    parser.add_argument('--epoch-num', nargs='+', type=int, default=None)

    return parser


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


if __name__ == "__main__":

    parser = parser_loader()
    args = vars(parser.parse_args())
    print(args)

    acc = np.zeros(1)
    times = np.zeros(1)

    for seed in range(1):
        setup_seed(seed)
        acc[seed], times[seed] = run(utils.ppi_loader, seed, **args)

    print('')
    print(np.mean(acc), np.mean(times))
    print(np.std(acc), np.std(times))

