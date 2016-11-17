﻿# Copyright (c) Microsoft. All rights reserved.

# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import os
import math
import numpy as np
import argparse

from cntk.blocks import default_options
from cntk.layers import Convolution, AveragePooling, GlobalAveragePooling, Dropout, BatchNormalization, Dense
from cntk.models import Sequential, LayerStack
from cntk.utils import *
from cntk.io import MinibatchSource, ImageDeserializer, StreamDef, StreamDefs, INFINITE_SAMPLES
from cntk.initializer import glorot_uniform, he_normal
from cntk import Trainer, distributed
from cntk.learner import momentum_sgd, learning_rate_schedule, UnitType, momentum_as_time_constant_schedule
from cntk.ops import cross_entropy_with_softmax, classification_error, relu
from cntk.ops import input_variable, constant, parameter, combine, times, element_times

#
# Paths relative to current python file.
#
abs_path   = os.path.dirname(os.path.abspath(__file__))
cntk_path  = os.path.normpath(os.path.join(abs_path, "..", "..", "..", "..", ".."))
data_path  = os.path.join(cntk_path, "Examples", "Image", "DataSets", "CIFAR-10")
model_path = os.path.join(abs_path, "Models")

# model dimensions
image_height = 32
image_width  = 32
num_channels = 3  # RGB
num_classes  = 10

#
# Define the reader for both training and evaluation action.
#
def create_reader(map_file, mean_file, train, distributed_after=INFINITE_SAMPLES):
    if not os.path.exists(map_file) or not os.path.exists(mean_file):
        raise RuntimeError("File '%s' or '%s' does not exist. Please run install_cifar10.py from Examples/Image/DataSets/CIFAR-10 to fetch them" %
                           (map_file, mean_file))

    # transformation pipeline for the features has jitter/crop only when training
    transforms = []
    if train:
        transforms += [
            ImageDeserializer.crop(crop_type='Random', ratio=0.8, jitter_type='uniRatio') # train uses jitter
        ]
    transforms += [
        ImageDeserializer.scale(width=image_width, height=image_height, channels=num_channels, interpolations='linear'),
        ImageDeserializer.mean(mean_file)
    ]
    # deserializer
    return MinibatchSource(ImageDeserializer(map_file, StreamDefs(
        features = StreamDef(field='image', transforms=transforms), # first column in map file is referred to as 'image'
        labels   = StreamDef(field='label', shape=num_classes))),      # and second as 'label'
        distributed_after = distributed_after)

#
# Resnet building blocks
#
#           ResNetNode                   ResNetNodeInc
#               |                              |
#        +------+------+             +---------+----------+
#        |             |             |                    |
#        V             |             V                    V
#   +----------+       |      +--------------+   +----------------+
#   | Conv, BN |       |      | Conv x 2, BN |   | SubSample, BN  |
#   +----------+       |      +--------------+   +----------------+
#        |             |             |                    |
#        V             |             V                    |
#    +-------+         |         +-------+                |
#    | ReLU  |         |         | ReLU  |                |
#    +-------+         |         +-------+                |
#        |             |             |                    |
#        V             |             V                    |
#   +----------+       |        +----------+              |
#   | Conv, BN |       |        | Conv, BN |              |
#   +----------+       |        +----------+              |
#        |             |             |                    |
#        |    +---+    |             |       +---+        |
#        +--->| + |<---+             +------>+ + +<-------+
#             +---+                          +---+
#               |                              |
#               V                              V
#           +-------+                      +-------+
#           | ReLU  |                      | ReLU  |
#           +-------+                      +-------+
#               |                              |
#               V                              V
#
def convolution_bn(input, filter_size, num_filters, strides=(1,1), init=he_normal(), activation=relu):
    if activation is None:
        activation = lambda x: x
        
    r = Convolution(filter_size, num_filters, strides=strides, init=init, activation=None, pad=True, bias=False)(input)
    r = BatchNormalization(map_rank=1)(r)
    r = activation(r)
    
    return r

def resnet_basic(input, num_filters):
    c1 = convolution_bn(input, (3,3), num_filters)
    c2 = convolution_bn(c1, (3,3), num_filters, activation=None)
    p  = c2 + input
    return relu(p)

def resnet_basic_inc(input, num_filters):
    c1 = convolution_bn(input, (3,3), num_filters, strides=(2,2))
    c2 = convolution_bn(c1, (3,3), num_filters, activation=None)

    s = convolution_bn(input, (1,1), num_filters, strides=(2,2), activation=None)
    
    p = c2 + s
    return relu(p)

def resnet_basic_stack(input, num_filters, num_stack):
    assert (num_stack > 0)

    r = input
    for _ in range(num_stack):
        r = resnet_basic(r, num_filters)
    return r

#   
# Defines the residual network model for classifying images
#
def create_resnet_model(input, num_classes):
    conv = convolution_bn(input, (3,3), 16)
    r1_1 = resnet_basic_stack(conv, 16, 3)

    r2_1 = resnet_basic_inc(r1_1, 32)
    r2_2 = resnet_basic_stack(r2_1, 32, 2)

    r3_1 = resnet_basic_inc(r2_2, 64)
    r3_2 = resnet_basic_stack(r3_1, 64, 2)

    pool = GlobalAveragePooling()(r3_2) 
    net = Dense(num_classes, init=he_normal(), activation=None)(pool)

    return net

#
# Train and evaluate the network.
#
def train_and_evaluate(reader_train, reader_test, max_epochs, distributed_trainer):

    # Input variables denoting the features and label data
    input_var = input_variable((num_channels, image_height, image_width))
    label_var = input_variable((num_classes))

    # Normalize the input
    feature_scale = 1.0 / 256.0
    input_var_norm = element_times(feature_scale, input_var)

    # apply model to input
    z = create_resnet_model(input_var_norm, 10)

    #
    # Training action
    #

    # loss and metric
    ce = cross_entropy_with_softmax(z, label_var)
    pe = classification_error(z, label_var)

    # training config
    epoch_size     = 50000
    minibatch_size = 128

    # Set learning parameters
    lr_per_minibatch       = learning_rate_schedule([1]*80 + [0.1]*40 + [0.01], UnitType.minibatch, epoch_size)
    momentum_time_constant = momentum_as_time_constant_schedule(-minibatch_size/np.log(0.9))
    l2_reg_weight          = 0.0001
    
    # trainer object
    learner     = momentum_sgd(z.parameters, 
                               lr = lr_per_minibatch, momentum = momentum_time_constant,
                               l2_regularization_weight = l2_reg_weight)
    trainer     = Trainer(z, ce, pe, learner, distributed_trainer)

    # define mapping from reader streams to network inputs
    input_map = {
        input_var: reader_train.streams.features,
        label_var: reader_train.streams.labels
    }

    log_number_of_parameters(z) ; print()
    progress_printer = ProgressPrinter(tag='Training')

    # perform model training
    for epoch in range(max_epochs):       # loop over epochs
        sample_count = 0
        while sample_count < epoch_size:  # loop over minibatches in the epoch
            data = reader_train.next_minibatch(min(minibatch_size, epoch_size - sample_count), input_map=input_map) # fetch minibatch.
            trainer.train_minibatch(data)                                   # update model with it

            sample_count += trainer.previous_minibatch_sample_count         # count samples processed so far
            progress_printer.update_with_trainer(trainer, with_metric=True) # log progress
        progress_printer.epoch_summary(with_metric=True)
        if distributed_trainer.communicator().current_worker().global_rank == 0:
            persist.save_model(z, os.path.join(model_path, "CifarResNet_Distributed_{}.dnn".format(epoch)))
    
    #
    # Evaluation action
    #
    epoch_size     = 10000
    minibatch_size = 16

    # process minibatches and evaluate the model
    metric_numer    = 0
    metric_denom    = 0
    sample_count    = 0
    minibatch_index = 0

    #progress_printer = ProgressPrinter(freq=100, first=10, tag='Eval')
    while sample_count < epoch_size:
        current_minibatch = min(minibatch_size, epoch_size - sample_count)

        # Fetch next test min batch.
        data = reader_test.next_minibatch(current_minibatch, input_map=input_map)

        # minibatch data to be trained with
        metric_numer += trainer.test_minibatch(data) * current_minibatch
        metric_denom += current_minibatch

        # Keep track of the number of samples processed so far.
        sample_count += data[label_var].num_samples
        minibatch_index += 1

    print("")
    print("Final Results: Minibatch[1-{}]: errs = {:0.1f}% * {}".format(minibatch_index+1, (metric_numer*100.0)/metric_denom, metric_denom))
    print("")

    # return evaluation error.
    return metric_numer/metric_denom

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-q', '--quantize_bit', help='quantized bit', required=False, default='32')
    parser.add_argument('-b', '--block_size', help='block momentum block size, quantized bit would be ignored if this is set', required=False)
    parser.add_argument('-e', '--epochs', help='total epochs', required=False, default='160')
    parser.add_argument('-w', '--warm_start', help='number of samples to warm start before running distributed', required=False, default='50000')
    args = vars(parser.parse_args())
    num_quantization_bits = int(args['quantize_bit'])
    epochs = int(args['epochs'])
    distributed_after_samples = int(args['warm_start'])
    
    if args['block_size']:
        block_size = int(args['block_size'])
        print("Start training:block_size = {}, epochs = {}, warm_start = {}".format(block_size, epochs, distributed_after_samples))
        distributed_trainer = distributed.block_momentum_distributed_trainer(
            block_size=block_size,
            distributed_after=distributed_after_samples)
    else:
        print("Start training: quantize_bit = {}, epochs = {}, warm_start = {}".format(num_quantization_bits, epochs, distributed_after_samples))
        distributed_trainer = distributed.data_parallel_distributed_trainer(
            num_quantization_bits=num_quantization_bits,
            distributed_after=distributed_after_samples)

    reader_train = create_reader(os.path.join(data_path, 'train_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), True, distributed_after_samples)
    reader_test  = create_reader(os.path.join(data_path, 'test_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), False)

    train_and_evaluate(reader_train, reader_test, max_epochs=epochs, distributed_trainer=distributed_trainer)
    distributed.Communicator.finalize()