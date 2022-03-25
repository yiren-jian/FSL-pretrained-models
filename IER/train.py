from __future__ import print_function

import os
import argparse
import socket
import time
import sys
from tqdm import tqdm
# import mkl
import torch
import torch.optim as optim
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.autograd import Variable
from dataset.transform_cfg import transforms_options, transforms_list
from models import model_pool
from models.util import create_model
from util import adjust_learning_rate, accuracy, AverageMeter, rotrate_concat, Logger, generate_final_report
from eval.meta_eval import meta_test, meta_test_tune
from eval.cls_eval import validate
import numpy as np
import wandb
from losses import simple_contrstive_loss
from dataloader import get_dataloaders


def parse_option():
    parser = argparse.ArgumentParser('argument for training')

    parser.add_argument('--eval_freq', type=int, default=10, help='meta-eval frequency')
    parser.add_argument('--print_freq', type=int, default=100, help='print frequency')
    parser.add_argument('--tb_freq', type=int, default=500, help='tb frequency')
    parser.add_argument('--save_freq', type=int, default=1, help='save frequency')
    parser.add_argument('--batch_size', type=int, default=64, help='batch_size')
    parser.add_argument('--num_workers', type=int, default=8, help='num of workers to use')
    parser.add_argument('--epochs', type=int, default=100, help='number of training epochs')

    # optimization
    parser.add_argument('--learning_rate', type=float, default=0.05, help='learning rate')
    parser.add_argument('--lr_decay_epochs', type=str, default='60,80', help='where to decay lr, can be a list')
    parser.add_argument('--lr_decay_rate', type=float, default=0.1, help='decay rate for learning rate')
    parser.add_argument('--weight_decay', type=float, default=5e-4, help='weight decay')
    parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
    parser.add_argument('--adam', action='store_true', help='use adam optimizer')
    parser.add_argument('--simclr', type=bool, default=False, help='use simple contrastive learning representation')
    parser.add_argument('--ssl', type=bool, default=True, help='use self supervised learning')
    parser.add_argument('--tags', type=str, default="gen0, ssl", help='add tags for the experiment')

    # dataset
    parser.add_argument('--model', type=str, default='resnet12', choices=model_pool)
    parser.add_argument('--dataset', type=str, default='miniImageNet', choices=['miniImageNet', 'tieredImageNet', 'CIFAR-FS', 'FC100'])
    parser.add_argument('--transform', type=str, default='A', choices=transforms_list)
    parser.add_argument('--use_trainval', type=bool, help='use trainval set')

    # cosine annealing
    parser.add_argument('--cosine', action='store_true', help='using cosine annealing')

    # specify folder
    parser.add_argument('--model_path', type=str, default='save/', help='path to save model')
    parser.add_argument('--tb_path', type=str, default='tb/', help='path to tensorboard')
    parser.add_argument('--data_root', type=str, default='/raid/data/IncrementLearn/imagenet/Datasets/MiniImagenet/', help='path to data root')

    # meta setting
    parser.add_argument('--n_test_runs', type=int, default=600, metavar='N', help='Number of test runs')
    parser.add_argument('--n_ways', type=int, default=5, metavar='N', help='Number of classes for doing each classification run')
    parser.add_argument('--n_shots', type=int, default=1, metavar='N', help='Number of shots in test')
    parser.add_argument('--n_queries', type=int, default=15, metavar='N', help='Number of query in test')
    parser.add_argument('--n_aug_support_samples', default=5, type=int, help='The number of augmented samples for each meta test sample')
    parser.add_argument('--test_batch_size', type=int, default=1, metavar='test_batch_size', help='Size of test batch)')
    parser.add_argument('-t', '--trial', type=str, default='1', help='the experiment id')

    #hyper parameters
    parser.add_argument('--gamma', type=float, default=1.0, help='loss cofficient for ssl loss')
    parser.add_argument('--contrast_temp', type=float, default=1.0, help='temperature for contrastive ssl loss')
    parser.add_argument('--membank_size', type=int, default=6400, help='temperature for contrastive ssl loss')
    parser.add_argument('--memfeature_size', type=int, default=64, help='temperature for contrastive ssl loss')
    parser.add_argument('--mvavg_rate', type=float, default=0.99, help='temperature for contrastive ssl loss')
    parser.add_argument('--trans', type=int, default=16, help='number of transformations')

    opt = parser.parse_args()

    if opt.dataset == 'CIFAR-FS' or opt.dataset == 'FC100':
        opt.transform = 'D'

    if opt.use_trainval:
        opt.trial = opt.trial + '_trainval'

    # set the path according to the environment
    if not opt.model_path:
        opt.model_path = './models_pretrained'
    if not opt.tb_path:
        opt.tb_path = './tensorboard'
    if not opt.data_root:
        opt.data_root = './data/{}'.format(opt.dataset)
    else:
        opt.data_root = '{}/{}'.format(opt.data_root, opt.dataset)
    opt.data_aug = True

    iterations = opt.lr_decay_epochs.split(',')
    opt.lr_decay_epochs = list([])
    for it in iterations:
        opt.lr_decay_epochs.append(int(it))

    tags = opt.tags.split(',')
    opt.tags = list([])
    for it in tags:
        opt.tags.append(it)

    opt.model_name = '{}_{}_lr_{}_decay_{}_trans_{}'.format(opt.model, opt.dataset, opt.learning_rate, opt.weight_decay, opt.transform)

    if opt.cosine:
        opt.model_name = '{}_cosine'.format(opt.model_name)

    if opt.adam:
        opt.model_name = '{}_useAdam'.format(opt.model_name)

    opt.model_name = '{}_trial_{}'.format(opt.model_name, opt.trial)

    opt.tb_folder = os.path.join(opt.tb_path, opt.model_name)
    if not os.path.isdir(opt.tb_folder):
        os.makedirs(opt.tb_folder)

    opt.save_folder = os.path.join(opt.model_path, opt.model_name)
    if not os.path.isdir(opt.save_folder):
        os.makedirs(opt.save_folder)

    opt.n_gpu = torch.cuda.device_count()

    #extras
    opt.fresh_start = True
    return opt


def main():

    opt = parse_option()

    train_loader, val_loader, meta_testloader, meta_valloader, n_cls, no_sample = get_dataloaders(opt)
    # model
    model = create_model(opt.model, n_cls, opt.dataset, n_trans=opt.trans, embd_sz=opt.memfeature_size)
    # wandb.watch(model)

    # optimizer
    if opt.adam:
        print("Adam")
        optimizer = torch.optim.Adam(model.parameters(),
                                     lr=opt.learning_rate,
                                     weight_decay=0.0005)
    else:
        print("SGD")
        optimizer = optim.SGD(model.parameters(),
                              lr=opt.learning_rate,
                              momentum=opt.momentum,
                              weight_decay=opt.weight_decay)

    criterion = nn.CrossEntropyLoss()

    if torch.cuda.is_available():
        if opt.n_gpu > 1:
            model = nn.DataParallel(model)
        model = model.cuda()
        criterion = criterion.cuda()
        cudnn.benchmark = True

    # set cosine annealing scheduler
    if opt.cosine:
        eta_min = opt.learning_rate * (opt.lr_decay_rate ** 3)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, opt.epochs, eta_min, -1)

    MemBank = np.random.randn(no_sample, opt.memfeature_size)
    MemBank = torch.tensor(MemBank, dtype=torch.float).cuda()
    MemBankNorm = torch.norm(MemBank, dim=1, keepdim=True)
    MemBank = MemBank / (MemBankNorm + 1e-6)

    # routine: supervised pre-training
    for epoch in range(1, opt.epochs + 1):
        if opt.cosine:
            scheduler.step()
        else:
            adjust_learning_rate(epoch, opt, optimizer)
        print("==> training...")

        time1 = time.time()
        train_acc, train_loss, MemBank = train(epoch, train_loader, model, criterion, optimizer, opt, MemBank)
        time2 = time.time()
        print('epoch {}, total time {:.2f}'.format(epoch, time2 - time1))

        val_acc, val_acc_top5, val_loss = 0,0,0 #validate(val_loader, model, criterion, opt)

        #validate
        start = time.time()
        meta_val_acc, meta_val_std = 0,0 #meta_test(model, meta_valloader)
        test_time = time.time() - start
        print('Meta Val Acc : {:.4f}, Meta Val std: {:.4f}, Time: {:.1f}'.format(meta_val_acc, meta_val_std, test_time))

        #evaluate
        start = time.time()
        meta_test_acc, meta_test_std = 0,0 #meta_test(model, meta_testloader)
        test_time = time.time() - start
        print('Meta Test Acc: {:.4f}, Meta Test std: {:.4f}, Time: {:.1f}'.format(meta_test_acc, meta_test_std, test_time))

        # regular saving
        if epoch % opt.save_freq == 0 or epoch==opt.epochs:
            print('==> Saving...')
            state = {
                'epoch': epoch,
                'optimizer': optimizer.state_dict(),
                'model': model.state_dict(),
            }
            save_file = os.path.join(opt.save_folder, 'model_epoch-%s.pth'%(str(epoch)))
            torch.save(state, save_file)

        print({'epoch': epoch,
                   'Train Acc': train_acc,
                   'Train Loss':train_loss,
                   'Val Acc': val_acc,
                   'Val Loss':val_loss,
                   'Meta Test Acc': meta_test_acc,
                   'Meta Test std': meta_test_std,
                   'Meta Val Acc': meta_val_acc,
                   'Meta Val std': meta_val_std
                  })

    #final report
    print("GENERATING FINAL REPORT")
    generate_final_report(model, opt)
    state = {
        'epoch': epoch,
        'optimizer': optimizer.state_dict(),
        'model': model.state_dict(),
    }
    save_file = os.path.join(opt.save_folder, 'model_final.pth')
    torch.save(state, save_file)


def train(epoch, train_loader, model, criterion, optimizer, opt, MemBank):
    """One epoch training"""
    model.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    train_indices = list(range(len(MemBank)))

    end = time.time()
    with tqdm(train_loader, total=len(train_loader)) as pbar:
        for _, (input, input2, input3, input4, target, indices) in enumerate(pbar):
            data_time.update(time.time() - end)

            if torch.cuda.is_available():
                input = input.cuda()
                input2 = input2.cuda()
                input3 = input3.cuda()
                input4 = input4.cuda()
                target = target.cuda()
                indices = indices.cuda()
            batch_size = input.shape[0]

            generated_data = rotrate_concat([input, input2, input3, input4])
            train_targets = target.repeat(opt.trans)
            proxy_labels = torch.zeros(opt.trans*batch_size).cuda().long()

            for ii in range(opt.trans):
                proxy_labels[ii*batch_size:(ii+1)*batch_size] = ii

            # ===================forward=====================
            _, (train_logit, eq_logit, inv_rep) = model(generated_data, inductive=True)

            # ===================memory bank of negatives for current batch=====================
            np.random.shuffle(train_indices)
            mn_indices_all = np.array(list(set(train_indices) - set(indices)))
            np.random.shuffle(mn_indices_all)
            mn_indices = mn_indices_all[:opt.membank_size]
            mn_arr = MemBank[mn_indices]
            mem_rep_of_batch_imgs = MemBank[indices]

            loss_ce = criterion(train_logit, train_targets)
            loss_eq = criterion(eq_logit, proxy_labels)

            inv_rep_0 = inv_rep[:batch_size, :]
            loss_inv = simple_contrstive_loss(mem_rep_of_batch_imgs, inv_rep_0, mn_arr, opt.contrast_temp)
            for ii in range(1, opt.trans):
                loss_inv += simple_contrstive_loss(inv_rep_0, inv_rep[(ii*batch_size):((ii+1)*batch_size), :], mn_arr, opt.contrast_temp)
            loss_inv = loss_inv/opt.trans

            loss = opt.gamma * (loss_eq + loss_inv) + loss_ce

            acc1, acc5 = accuracy(train_logit, train_targets, topk=(1, 5))
            losses.update(loss.item(), input.size(0))
            top1.update(acc1[0], input.size(0))
            top5.update(acc5[0], input.size(0))

            # ===================update memory bank======================
            MemBankCopy = MemBank.clone().detach()
            MemBankCopy[indices] = (opt.mvavg_rate * MemBankCopy[indices]) + ((1 - opt.mvavg_rate) * inv_rep_0)
            MemBank = MemBankCopy.clone().detach()

            # ===================backward=====================
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # ===================meters=====================
            batch_time.update(time.time() - end)
            end = time.time()

            pbar.set_postfix({"Acc@1":'{0:.2f}'.format(top1.avg.cpu().numpy()),
                              "Acc@5":'{0:.2f}'.format(top5.avg.cpu().numpy(),2),
                              "Loss" :'{0:.2f}'.format(losses.avg,2),
                             })

    print('Train_Acc@1 {top1.avg:.3f} Train_Acc@5 {top5.avg:.3f}'.format(top1=top1, top5=top5))

    return top1.avg, losses.avg, MemBank


if __name__ == '__main__':
    main()
