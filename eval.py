import torch
import os
import re
import time
import yaml
import numpy as np
import argparse
import pickle
from easydict import EasyDict as edict
from torch.utils.data import DataLoader

from prefetch_generator import BackgroundGenerator
from dataset import HICO_test_set
from model import AE, IDN
from utils import Timer, HO_weight, AverageMeter
from HICO_DET_utils import obj_range, get_map


class DataLoaderX(DataLoader):
    def __iter__(self):
        return BackgroundGenerator(super().__iter__())

models = {
    'AE': AE,
    'IDN': IDN,
}

gpus = list(range(len(os.environ['CUDA_VISIBLE_DEVICES'].split(','))))
device = torch.device('cuda:{}'.format(gpus[0]))

def parse_arg():
    parser = argparse.ArgumentParser(description='Generate detection file')
    parser.add_argument('--exp', dest='exp',
            help='Define exp name',
            default='_'.join(time.asctime(time.localtime(time.time())).split()), type=str)
    parser.add_argument('--config_path', dest='config_path',
            help='Select config file',
            default='configs/default_eval.yml', type=str)
    args = parser.parse_args()
    return args
    
def get_config(args):
    loader = yaml.FullLoader
    loader.add_implicit_resolver(
        u'tag:yaml.org,2002:float',
        re.compile(u'''^(?:
         [-+]?(?:[0-9][0-9_]*)\\.[0-9_]*(?:[eE][-+]?[0-9]+)?
        |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
        |\\.[0-9_]+(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\\.[0-9_]*
        |[-+]?\\.(?:inf|Inf|INF)
        |\\.(?:nan|NaN|NAN))$''', re.X),
        list(u'-+0123456789.'))
    config = edict(yaml.load(open(args.config_path, 'r'), Loader=loader))
    return config

args = parse_arg()

cur_path = os.path.join(os.getcwd(), 'exp', args.exp)
if not os.path.exists(cur_path):
    os.mkdir(cur_path)

config = get_config(args)
yaml.dump(dict(config), open(os.path.join(cur_path, 'config.yml'), 'w'))

test_set    = HICO_test_set(config.TRAIN.DATA_DIR, split='test')
test_loader = DataLoaderX(test_set, batch_size=config.TEST.BATCH_SIZE, shuffle=False, num_workers=config.TEST.NUM_WORKERS, collate_fn=test_set.collate_fn, pin_memory=False, drop_last=False)

net = models[config.MODE](config.MODEL, HO_weight)
if len(gpus) > 1:
    net = torch.nn.DataParallel(net.to(device), device_ids=gpus, output_device=gpus[0])
else:
    net = net.to(device)

test_timer = Timer()

def eval(net, loader, timer):
    net.eval()

    verb_mapping = torch.from_numpy(pickle.load(open('verb_mapping.pkl', 'rb'), encoding='latin1')).float()

    bboxes, scores, scores_AE, scores_rev, keys, hdet, odet = [], [], [], [], [], [], []
    for i in range(80):
        bboxes.append([])
        scores.append([])
        scores_AE.append([])
        scores_rev.append([])
        keys.append([])
        hdet.append([])
        odet.append([])
    
    timer.tic()
    for i, batch in enumerate(loader):
        
        n = batch['shape'].shape[0]
        batch['shape']   = batch['shape'].cuda(non_blocking=True)
        batch['spatial'] = batch['spatial'].cuda(non_blocking=True)
        batch['sub_vec'] = batch['sub_vec'].cuda(non_blocking=True)
        batch['obj_vec'] = batch['obj_vec'].cuda(non_blocking=True)
        batch['uni_vec'] = batch['uni_vec'].cuda(non_blocking=True)
        batch['labels_s']   = batch['labels_s'].cuda(non_blocking=True)
        batch['labels_ro']  = batch['labels_ro'].cuda(non_blocking=True)
        batch['labels_r']   = batch['labels_r'].cuda(non_blocking=True)
        batch['labels_sro'] = batch['labels_sro'].cuda(non_blocking=True)
        verb_mapping    = verb_mapping.cuda(non_blocking=True)
        output = net(batch)
        
        batch['spatial'][:, 0] *= batch['shape'][:, 0]
        batch['spatial'][:, 1] *= batch['shape'][:, 1]
        batch['spatial'][:, 2] *= batch['shape'][:, 0]
        batch['spatial'][:, 3] *= batch['shape'][:, 1]
        batch['spatial'][:, 4] *= batch['shape'][:, 0]
        batch['spatial'][:, 5] *= batch['shape'][:, 1]
        batch['spatial'][:, 6] *= batch['shape'][:, 0]
        batch['spatial'][:, 7] *= batch['shape'][:, 1]
        obj_class = batch['obj_class']
        bbox = batch['spatial'].detach().cpu().numpy()

        if 's' in output:
            output['s'] = torch.matmul(output['s'], verb_mapping)
            output['s'] = output['s'].detach().cpu().numpy()
        if 's_AE' in output:
            output['s_AE'] = torch.matmul(output['s_AE'], verb_mapping)
            output['s_AE'] = output['s_AE'].detach().cpu().numpy()
        if 's_rev' in output:
            output['s_rev'] = torch.matmul(output['s_rev'], verb_mapping)
            output['s_rev'] = output['s_rev'].detach().cpu().numpy()

        for j in range(bbox.shape[0]):
            cls = obj_class[j]
            x, y = obj_range[cls][0]-1, obj_range[cls][1]
            keys[cls].append(batch['key'][j])
            bboxes[cls].append(bbox[j])
            if 's' in output:
                scores[cls].append(output['s'][j, x:y])
            if 's_AE' in output:
                scores_AE[cls].append(output['s_AE'][j, x:y])
            if 's_rev' in output:
                scores_rev[cls].append(output['s_rev'][j, x:y])
            hdet[cls].append(batch['hdet'][j])
            odet[cls].append(batch['odet'][j])
        timer.toc()
        if i % 1000 == 0:
            print("%05d iteration, average time %.4f" % (i, timer.average_time))
        timer.tic()
    timer.toc()
    for i in range(80):
        keys[i]       = np.array(keys[i])
        bboxes[i]     = np.array(bboxes[i])
        scores[i]     = np.array(scores[i])
        scores_AE[i]  = np.array(scores_AE[i])
        scores_rev[i] = np.array(scores_rev[i])
        hdet[i]       = np.array(hdet[i])
        odet[i]       = np.array(odet[i])
    res = {
        'keys': keys,
        'bboxes': bboxes,
        'scores': scores,
        'scores_AE': scores_AE,
        'scores_rev': scores_rev,
        'hdet': hdet,
        'odet': odet,
    }
    return res

res = eval(net, test_loader, test_timer)
pickle.dump(res, open(os.path.join(cur_path, 'result.pkl'), 'wb'))