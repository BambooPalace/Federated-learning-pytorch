#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6

import time
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from train import criterion, evaluate


class DatasetSplit(Dataset):
    """An abstract Dataset class wrapped around Pytorch Dataset class.
    """

    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = [int(i) for i in idxs]

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        # return torch.tensor(image), torch.tensor(label)
        # pytorch warning and suggest below 
        return image.clone().detach(), label.clone().detach()


class LocalUpdate(object):
    def __init__(self, args, dataset, idxs):
        self.args = args
        self.trainloader, self.testloader = self.train_val_test(
            dataset, list(idxs))
        self.device = 'cuda' if torch.cuda.is_available() and not args.cpu_only else 'cpu'
        # Default criterion set to NLL loss function

    def train_val_test(self, dataset, idxs):
        """
        Returns train, validation and test dataloaders for a given dataset
        and user indexes.
        """
        # split indexes for train, and test (80, 20)
        idxs_train = idxs[:int(0.8*len(idxs))]
        idxs_test = idxs[int(0.8*len(idxs)):]

        # mod1: add num_workers, to see if can speed up training. ANS is no for cifar
        trainloader = DataLoader(DatasetSplit(dataset, idxs_train),
                                 batch_size=self.args.local_bs, num_workers=self.args.num_workers, shuffle=True)
        testloader = DataLoader(DatasetSplit(dataset, idxs_test),
                                batch_size=max(len(idxs_test)//10,1), num_workers=self.args.num_workers, shuffle=False)
        return trainloader, testloader

    def update_weights(self, model, global_round):
        # Set mode to train model
        model.train()
        epoch_loss = []

        # Set optimizer and lr_scheduler for the local updates
        args = self.args
        if args.aux_lr_param > 1:
            params_to_optimize = [
            {"params": [p for p in model.backbone.parameters() if p.requires_grad]},
            {"params": [p for p in model.classifier.parameters() if p.requires_grad]}]
            if model.aux_classifier:
                params = [p for p in model.aux_classifier.parameters() if p.requires_grad]
                params_to_optimize.append({"params": params, "lr": args.lr * args.aux_lr_param}) #multiplier default is 10
        else:
            params_to_optimize = [p for p in model.parameters() if p.requires_grad]

        if args.optimizer == 'sgd':
            optimizer = torch.optim.SGD(params_to_optimize, lr=args.lr,
                                        momentum=args.momentum, weight_decay=0.0001)
        elif args.optimizer == 'adam':
            optimizer = torch.optim.Adam(params_to_optimize, lr=args.lr,
                                        weight_decay=1e-4)

        scheduler_dict = {
            'step': torch.optim.lr_scheduler.StepLR(optimizer, step_size=3, gamma=0.1),
            'lambda':torch.optim.lr_scheduler.LambdaLR(optimizer, lambda x: (1 - x / (len(self.trainloader)*max(1,args.epochs))) ** 0.9)
        }
        lr_scheduler = scheduler_dict[args.lr_scheduler]                                                                                 

        # training
        start_time = time.time()
        for iter in range(self.args.local_ep):
            batch_loss = []
            for batch_idx, (images, labels) in enumerate(self.trainloader):
                images, labels = images.to(self.device), labels.to(self.device)

                model.zero_grad()
                log_probs = model(images)
                loss = criterion(log_probs, labels)
                loss.backward()
                optimizer.step()

                batch_loss.append(loss.item())
            epoch_loss.append(sum(batch_loss)/len(batch_loss))
            if self.args.verbose:
                print('| Global Round : {} | Local Epoch : {} | {} images\tLoss: {:.6f}'.format(
                    global_round, iter,
                    len(self.trainloader.dataset),loss.item()))
            lr_scheduler.step()
        print('| Global Round : {} | Local Epochs : {} | {} images\tLoss: {:.6f}'.format(
            global_round, self.args.local_ep,
            len(self.trainloader.dataset), loss.item()))
        print('\n Run Time: {0:0.4f}'.format(time.time()-start_time))
        return model.state_dict(), sum(epoch_loss) / len(epoch_loss)


    def inference(self, model):
        """ Returns the inference accuracy and loss.
        """
        confmat = evaluate(model, self.testloader, self.device, self.args.num_classes)
        return confmat.acc_global, confmat.iou_mean


def test_inference(args, model, testloader):
    """ Returns the test accuracy and loss.
    """

    model.eval()
    device = 'cuda' if torch.cuda.is_available() and not args.cpu_only else 'cpu'
    confmat = evaluate(model, testloader, device, args.num_classes)

    return confmat.acc_global, confmat.iou_mean