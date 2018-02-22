from __future__ import print_function
import numpy as np
import os
import sys
import cv2
import pickle

import torch
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import torch.optim as optim
from torch.optim import lr_scheduler
import torch.utils.data as data
import torch.nn.init as init

from tensorboardX import SummaryWriter

from lib.layers import *
from lib.utils.timer import Timer
from lib.utils.nms.nms_wrapper import nms
from lib.dataset.data_augment import preproc, BaseTransform

from lib.modeling.model_builder import create_model
from lib.dataset.dataset_factory import load_data
from lib.utils.config_parse import cfg
from lib.utils.eval_utils import *

class Solver(object):
    """
    A wrapper class for the training process
    """
    def __init__(self):
        self.cfg = cfg

        # Load data
        print('===> Loading data')
        self.train_loader = load_data(cfg.DATASET, 'train') if 'train' in cfg.PHASE else None
        self.eval_loader = load_data(cfg.DATASET, 'eval') if 'eval' in cfg.PHASE else None
        self.test_loader = load_data(cfg.DATASET, 'test') if 'test' in cfg.PHASE else None

        # Build model
        print('===> Building model')
        self.model, self.priors = create_model(cfg.MODEL)
        self.detector = Detect(cfg.POST_PROCESS, self.priors)

        # Utilize GPUs for computation
        self.use_gpu = torch.cuda.is_available()
        if self.use_gpu:
            print('Utilize GPUs for computation')
            print('Number of GPU available', torch.cuda.device_count())
            self.model.cuda()
            self.priors.cuda()
            cudnn.benchmark = True
            # self.model = torch.nn.DataParallel(self.model).module

        # Print the model architecture and parameters
        print('Model architectures:\n{}\n'.format(self.model))

        print('Parameters and size:')
        for name, param in self.model.named_parameters():
            print('{}: {}'.format(name, list(param.size())))

        # print trainable scope
        # print('Trainable scope:')
        trainable_param = self.trainable_param(cfg.TRAIN.TRAINABLE_SCOPE)
        self.optimizer = optim.SGD(trainable_param, lr=cfg.TRAIN.LEARNING_RATE,
                        momentum=cfg.TRAIN.MOMENTUM, weight_decay=cfg.TRAIN.WEIGHT_DECAY)
        self.exp_lr_scheduler = lr_scheduler.StepLR(self.optimizer, step_size=cfg.TRAIN.STEPSIZE, gamma=cfg.TRAIN.GAMMA)
        self.max_epochs = cfg.TRAIN.MAX_EPOCHS

        # metric
        self.criterion = MultiBoxLoss(cfg.MATCHER, self.priors, self.use_gpu)

        # Set the logger
        self.writer = SummaryWriter(log_dir=cfg.LOG_DIR)
        self.output_dir = cfg.EXP_DIR
        self.resume_checkpoint = cfg.RESUME_CHECKPOINT
        self.checkpoint_prefix = cfg.CHECKPOINTS_PREFIX


    def save_checkpoints(self, epochs, iters=None):
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        if iters:
            filename = self.checkpoint_prefix + '_epoch_{:d}_iter_{:d}'.format(epochs, iters) + '.pth'
        else:
            filename = self.checkpoint_prefix + '_epoch_{:d}'.format(epochs) + '.pth'
        filename = os.path.join(self.output_dir, filename)
        torch.save(self.model.state_dict(), filename)
        with open(os.path.join(self.output_dir, 'checkpoint_list.txt'), 'a') as f:
            f.write('epoch {epoch:d}: {filename}\n'.format(epoch=epochs, filename=filename))
        print('Wrote snapshot to: {:s}'.format(filename))
        
        # TODO: write relative cfg under the same page
    
    def restore_checkpoint(self, resume_checkpoint):
        if resume_checkpoint == '':
            return False
        print('Restoring checkpoint from {:s}'.format(resume_checkpoint))
        return self.model.load_weights(resume_checkpoint, cfg.TRAIN.RESUME_SCOPE)

    def find_previous(self):
        if not os.path.exists(os.path.join(self.output_dir, 'checkpoint_list.txt')):
            return False
        with open(os.path.join(self.output_dir, 'checkpoint_list.txt'), 'r') as f:
            lineList = f.readlines()
        line = lineList[-1]
        start_epoch = int(line[line.find('epoch ') + len('epoch '): line.find(':')])
        resume_checkpoint = line[line.find(':') + 2:-1]
        return start_epoch, resume_checkpoint

    def weights_init(self, m):
        for key in m.state_dict():
            if key.split('.')[-1] == 'weight':
                if 'conv' in key:
                    init.kaiming_normal(m.state_dict()[key], mode='fan_out')
                if 'bn' in key:
                    m.state_dict()[key][...] = 1
            elif key.split('.')[-1] == 'bias':
                m.state_dict()[key][...] = 0

    def initialize(self):
        # Fresh train directly from ImageNet weights
        if self.resume_checkpoint:
            print('Loading initial model weights from {:s}'.format(self.resume_checkpoint))
            self.restore_checkpoint(self.resume_checkpoint)        
        else:
        # TODO: ADD INIT ways
            self.model.extras.apply(self.weights_init)
            self.model.loc.apply(self.weights_init)
            self.model.conf.apply(self.weights_init)
        start_epoch = 0
        return start_epoch
    
    def trainable_param(self, trainable_scope):
        for param in self.model.parameters():
            param.requires_grad = False

        trainable_param = []
        for module in trainable_scope.split(','):
            if hasattr(self.model, module):
                # print(getattr(self.model, module))
                for param in getattr(self.model, module).parameters():
                    param.requires_grad = True
                trainable_param.extend(getattr(self.model, module).parameters())
                    
        return trainable_param

    def train_model(self):
        previous = self.find_previous()
        if previous:
            start_epoch = previous[0]
            self.restore_checkpoint(previous[1])
        else:
            start_epoch = self.initialize()

        # export graph for the model, onnx always not works
        self.export_graph()

        for epoch in iter(range(start_epoch+1, self.max_epochs)):
            #learning rate
            sys.stdout.write('\rEpoch {epoch:d}/{max_epochs:d}:\n'.format(epoch=epoch, max_epochs=self.max_epochs))
            self.exp_lr_scheduler.step(epoch)
            if 'train' in cfg.PHASE:
                self.train_epoch(self.model, self.train_loader, self.optimizer, self.criterion, self.writer, epoch, self.use_gpu)
            if 'eval' in cfg.PHASE:
                self.eval_epoch(self.model, self.eval_loader, self.detector, self.criterion, self.writer, epoch, self.use_gpu)
            if 'test' in cfg.PHASE:
                self.test_epoch(self.model, self.eval_loader, self.detector, self.output_dir , self.use_gpu)

            if epoch % cfg.TRAIN.CHECKPOINTS_EPOCHS == 0:
                self.save_checkpoints(epoch)


    def train_epoch(self, model, data_loader, optimizer, criterion, writer, epoch, use_gpu):
        # sys.stdout.write('\r===> Training mode\n')
        
        model.train()

        epoch_size = len(data_loader)
        batch_iterator = iter(data_loader)

        loc_loss = 0
        conf_loss = 0
        _t = Timer()

        for iteration in iter(range((epoch_size))):
            images, targets = next(batch_iterator)
            if use_gpu:
                images = Variable(images.cuda())
                targets = [Variable(anno.cuda(), volatile=True) for anno in targets]
            else:
                images = Variable(images)
                targets = [Variable(anno, volatile=True) for anno in targets]
            print('targets',targets)
            _t.tic()
            # forward
            out = model(images, is_train=True)

            # backprop
            optimizer.zero_grad()
            loss_l, loss_c = criterion(out, targets)
            loss = loss_l + loss_c
            loss.backward()
            optimizer.step()

            time = _t.toc()
            loc_loss += loss_l.data[0]
            conf_loss += loss_c.data[0]

            # log per iter
            log = '\r==>Train: || {iters:d}/{epoch_size:d} in {time:.2f}s [{prograss}] || loc_loss: {loc_loss:.4f} cls_loss: {cls_loss:.4f}\r'.format(
                    prograss='#'*int(round(10*iteration/epoch_size)) + '-'*int(round(10*(1-iteration/epoch_size))), iters=iteration, epoch_size=epoch_size, 
                    time=time, loc_loss=loss_l.data[0], cls_loss=loss_c.data[0])
            
            sys.stdout.write(log)
            sys.stdout.flush()

        # log per epoch
        sys.stdout.write('\r')
        sys.stdout.flush()
        lr = optimizer.param_groups[0]['lr']
        log = '\r==>Train: || Total_time: {time:.2f}s || loc_loss: {loc_loss:.4f} conf_loss: {conf_loss:.4f} || lr: {lr:.6f}\n'.format(lr=lr,
                time=_t.total_time, loc_loss=loc_loss/epoch_size, conf_loss=conf_loss/epoch_size)
        sys.stdout.write(log)
        sys.stdout.flush()
        
        # log for tensorboard
        writer.add_scalar('Train/loc_loss', loc_loss/epoch_size, epoch)
        writer.add_scalar('Train/conf_loss', conf_loss/epoch_size, epoch)
        writer.add_scalar('Train/lr', lr, epoch)

    
    def eval_epoch(self, model, data_loader, detector, criterion, writer, epoch, use_gpu):
        # sys.stdout.write('\r===> Eval mode\n')
        
        model.eval()

        epoch_size = len(data_loader)
        batch_iterator = iter(data_loader)

        loc_loss = 0
        conf_loss = 0
        _t = Timer()

        label = [list() for _ in range(model.num_classes)]
        score = [list() for _ in range(model.num_classes)]
        npos = [0] * model.num_classes

        for iteration in iter(range((epoch_size))):
            images, targets = next(batch_iterator)
            if use_gpu:
                images = Variable(images.cuda())
                targets = [Variable(anno.cuda(), volatile=True) for anno in targets]
            else:
                images = Variable(images)
                targets = [Variable(anno, volatile=True) for anno in targets]

            _t.tic()
            # forward
            out = model(images, is_train=True)

            # loss
            loss_l, loss_c = criterion(out, targets)

            out = (out[0], model.softmax(out[1].view(-1, model.num_classes)))

            # detect
            detections = detector.forward(out)

            time = _t.toc()

            # evals
            label, score, npos = cal_tp_fp(detections, targets, label, score, npos)
            loc_loss += loss_l.data[0]
            conf_loss += loss_c.data[0]

            # log per iter
            log = '\r==>Eval: || {iters:d}/{epoch_size:d} in {time:.2f}s [{prograss}] || loc_loss: {loc_loss:.4f} cls_loss: {cls_loss:.4f}\r'.format(
                    prograss='#'*int(round(10*iteration/epoch_size)) + '-'*int(round(10*(1-iteration/epoch_size))), iters=iteration, epoch_size=epoch_size, 
                    time=time, loc_loss=loss_l.data[0], cls_loss=loss_c.data[0])
            
            sys.stdout.write(log)
            sys.stdout.flush()

        # eval mAP
        prec, rec, ap = cal_pr(label, score, npos)

        # log per epoch
        sys.stdout.write('\r')
        sys.stdout.flush()
        log = '\r==>Eval: || Total_time: {time:.2f}s || loc_loss: {loc_loss:.4f} conf_loss: {conf_loss:.4f} || mAP: {mAP:.6f}\n'.format(mAP=ap,
                time=_t.total_time, loc_loss=loc_loss/epoch_size, conf_loss=conf_loss/epoch_size)
        sys.stdout.write(log)
        sys.stdout.flush()
        
        # log for tensorboard
        writer.add_scalar('Eval/loc_loss', loc_loss/epoch_size, epoch)
        writer.add_scalar('Eval/conf_loss', conf_loss/epoch_size, epoch)
        writer.add_scalar('Eval/mAP', ap, epoch)
        # self.draw_pr()

    # TODO: HOW TO MAKE THE DATALOADER WITHOUT SHUFFLE
    # def test_epoch(self, model, data_loader, detector, output_dir, use_gpu):
    #     # sys.stdout.write('\r===> Eval mode\n')
        
    #     model.eval()

    #     num_images = len(data_loader.dataset)
    #     num_classes = detector.num_classes
    #     batch_size = data_loader.batch_size
    #     all_boxes = [[[] for _ in range(num_images)] for _ in range(num_classes)]
    #     empty_array = np.transpose(np.array([[],[],[],[],[]]),(1,0))

    #     epoch_size = len(data_loader)
    #     batch_iterator = iter(data_loader)

    #     _t = Timer()

    #     for iteration in iter(range((epoch_size))):
    #         images, targets = next(batch_iterator)
    #         targets = [[anno[0][1], anno[0][0], anno[0][1], anno[0][0]] for anno in targets] # contains the image size
    #         if use_gpu:
    #             images = Variable(images.cuda())
    #         else:
    #             images = Variable(images)

    #         _t.tic()
    #         # forward
    #         out = model(images, is_train=False)

    #         # detect
    #         detections = detector.forward(out)

    #         time = _t.toc()

    #         # TODO: make it smart:
    #         for i, (dets, scale) in enumerate(zip(detections, targets)):
    #             for j in range(1, num_classes):
    #                 cls_dets = list()
    #                 for det in dets[j]:
    #                     if det[0] > 0:
    #                         d = det.cpu().numpy()
    #                         score, box = d[0], d[1:]
    #                         box *= scale
    #                         box = np.append(box, score)
    #                         cls_dets.append(box)
    #                 if len(cls_dets) == 0:
    #                     cls_dets = empty_array
    #                 all_boxes[j][iteration*batch_size+i] = np.array(cls_dets)

    #         # log per iter
    #         log = '\r==>Test: || {iters:d}/{epoch_size:d} in {time:.2f}s [{prograss}]\r'.format(
    #                 prograss='#'*int(round(10*iteration/epoch_size)) + '-'*int(round(10*(1-iteration/epoch_size))), iters=iteration, epoch_size=epoch_size, 
    #                 time=time)
    #         sys.stdout.write(log)
    #         sys.stdout.flush()

    #     # write result to pkl
    #     with open(os.path.join(output_dir, 'detections.pkl'), 'wb') as f:
    #         pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)
        
    #     print('Evaluating detections')
    #     data_loader.dataset.evaluate_detections(all_boxes, output_dir)


    def test_epoch(self, model, data_loader, detector, output_dir, use_gpu):
        # sys.stdout.write('\r===> Eval mode\n')
        
        model.eval()

        dataset = data_loader.dataset
        num_images = len(dataset)
        num_classes = detector.num_classes
        all_boxes = [[[] for _ in range(num_images)] for _ in range(num_classes)]
        empty_array = np.transpose(np.array([[],[],[],[],[]]),(1,0))

        _t = Timer()

        for i in iter(range((num_images))):
            img = dataset.pull_image(i)
            scale = [img.shape[1], img.shape[0], img.shape[1], img.shape[0]]
            if use_gpu:
                images = Variable(dataset.preproc(img).unsqueeze(0).cuda(), volatile=True)
            else:
                images = Variable(dataset.preproc(img).unsqueeze(0), volatile=True)

            _t.tic()
            # forward
            out = model(images, is_train=False)

            # detect
            detections = detector.forward(out)

            time = _t.toc()

            # TODO: make it smart:
            for j in range(1, num_classes):
                cls_dets = list()
                for det in detections[0][j]:
                    if det[0] > 0:
                        d = det.cpu().numpy()
                        score, box = d[0], d[1:]
                        box *= scale
                        box = np.append(box, score)
                        cls_dets.append(box)
                if len(cls_dets) == 0:
                    cls_dets = empty_array
                all_boxes[j][i] = np.array(cls_dets)

            # log per iter
            log = '\r==>Test: || {iters:d}/{epoch_size:d} in {time:.2f}s [{prograss}]\r'.format(
                    prograss='#'*int(round(10*i/num_images)) + '-'*int(round(10*(1-i/num_images))), iters=i, epoch_size=num_images, 
                    time=time)
            sys.stdout.write(log)
            sys.stdout.flush()

        # write result to pkl
        with open(os.path.join(output_dir, 'detections.pkl'), 'wb') as f:
            pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)
        
        print('Evaluating detections')
        data_loader.dataset.evaluate_detections(all_boxes, output_dir)


    def export_graph(self):
        dummy_input = Variable(torch.randn(10, 3, cfg.MODEL.IMAGE_SIZE[0], cfg.MODEL.IMAGE_SIZE[1])).cuda()
        if not os.path.exists(cfg.EXP_DIR):
            os.makedirs(cfg.EXP_DIR)
        # torch.onnx.export(self.model, dummy_input, os.path.join(cfg.EXP_DIR, "graph.pd"))
        self.writer.add_graph(self.model, (dummy_input, ))


    def draw_pr(self, precision, recall, iter):
        for i, (prec, rec) in enumerate(zip(precision, recall)):
            num_thresholds = min(500, len(prec))
            if num_thresholds != len(prec):
                gap = len(prec) / num_thresholds
                _prec = np.append(prec[::gap], prec[-1])
                _rec  = np.append(rec[::gap], rec[-1])
                num_thresholds = len(_prec)
            # the pr_curve_raw_data_pb() needs the a ascending precisions array and a descending recalls array
            _prec[::-1].sort()
            _rec[::-1].sort()
            #TODO: This one is not correct.
            # self.writer.add_pr_curve(tag=i, _prec, _rec, iter)


    def draw_bounding_box(self, img, ground_truth, predictions, iteration, tag='image'):
        scale = torch.Tensor([img.shape[1], img.shape[0],
                             img.shape[1], img.shape[0]])
        pred_bbxs = get_correct_detection(predictions, ground_truth) * scale
        gt_bbxs = ground_truth[0] * scale
        for gt_bbxs_c in gt_bbxs:
            for bbx in gt_bbxs_c:
                cv2.rectangle(img, (bbx[0], bbx[1]), (bbx[2], bbx[3]), (0, 0, 255, 5), 5)
        for pred_bbxs_c in pred_bbxs:
            for bbx in pred_bbxs_c:
                cv2.rectangle(img, (bbx[0], bbx[1]), (bbx[2], bbx[3]), (0, 255, 0, 5), 5)
        self.writer.add_image(tag, img, iteration)

    def predict(self, img):
        return True


def train_model():
    sw = Solver()
    sw.train_model()
    return True