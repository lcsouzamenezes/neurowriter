#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jul 28 11:23:34 2017

Definitions of different text generation models.

Models are defined in a way that when multiple GPUs are present in the
host, model parallelization is performed for faster training.

@author: Álvaro Barbero Jiménez
"""

from keras.models import Sequential, Model
from keras.layers import Conv1D, MaxPooling1D, Dense, Flatten, Input, Dropout
from keras.layers import add, multiply
from keras.layers.advanced_activations import ELU
from keras.layers.recurrent import LSTM
import tensorflow as tf
from tensorflow.python.client import device_lib

def modelbyname(modelname):
    """Returns a model generating class by name"""
    models = {
        "dilatedconv" : DilatedConvModel,
        "wavenet" : WavenetModel,
        "lstm" : LSTMModel
    }
    if modelname not in models:
        raise ValueError("Unknown model %s" % modelname)
    return models[modelname]

def get_available_gpus():
    """Returns a list of the GPU devices found in the host
    
    Reference: 
        - https://stackoverflow.com/questions/38559755/how-to-get-current-available-gpus-in-tensorflow
    """
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

class DilatedConvModel():
    """Model based on dilated convolutions + pooling + dense layers"""
    
    paramgrid = [
        [2,3,4,5], # convlayers
        [4,8,16,32,64], # kernels
        (0.0, 1.0), # convdrop
        [0,1,2,3], # denselayers
        [16,32,64,128,256], # dense units
        (0.0, 1.0), # densedrop
        ['sgd', 'rmsprop', 'adam'], # optimizer
    ]
    
    def create(inputtokens, encoder, convlayers=5, kernels = 32,
               convdrop=0.1, denselayers=0, denseunits=64, densedrop=0.1,
               optimizer='adam'):
        kernel_size = 2
        pool_size = 2
        if convlayers < 1:
            raise ValueError("Number of layers must be at least 1")
            
        # First conv+pool layer
        model = Sequential()
        model.add(Conv1D(kernels, kernel_size, padding='causal', activation='relu', 
                         input_shape=(inputtokens, encoder.nchars)))
        model.add(Dropout(convdrop))
        model.add(MaxPooling1D(pool_size))
        # Additional dilated conv + pool layers (if possible)
        for i in range(1, convlayers):
            try:
                model.add(Conv1D(kernels, kernel_size, padding='causal', 
                                 dilation_rate=2**i, activation='relu'))
                model.add(Dropout(convdrop))
                model.add(MaxPooling1D(pool_size))
            except:
                print("Warning: not possible to add %i-th layer, moving to output" % i)
                break
                
        # Flatten and dense layers
        model.add(Flatten())
        for i in range(denselayers):
            model.add(Dense(denseunits, activation='relu'))
            model.add(Dropout(densedrop))
        # Output layer
        model.add(Dense(encoder.nchars, activation='softmax'))
        model.compile(optimizer=optimizer, loss='categorical_crossentropy', 
                      metrics=['accuracy'])
        return model
    
class WavenetModel():
    """Implementation of Wavenet model
    
    The model is made of a series of blocks, each one made up of 
    exponentially increasing dilated convolutions, until the whole input
    sequence is covered. Residual connections are also included to speed
    up training
    
    As an addition to the original formulation, ReLU activations have
    been replaced by ELU units.
    
    This implementation is based on those provided by
        - https://github.com/basveeling/wavenet
        - https://github.com/usernaamee/keras-wavenet
        
    The original wavenet paper is available at
        - https://deepmind.com/blog/wavenet-generative-model-raw-audio/
        - https://arxiv.org/pdf/1609.03499.pdf
    """
    
    paramgrid = [
        [32,64,128,256], # kernels
        [1,2,3,4,5], # wavenetblocks
        (0.0, 1.0), # dropout
        ['sgd', 'rmsprop', 'adam'], # optimizer
    ]
    
    def create(inputtokens, encoder, kernels=64, wavenetblocks=1, 
               dropout=0, optimizer='adam'):
        kernel_size = 2
        maxdilation = inputtokens
        gpus = get_available_gpus()
        
        def gatedblock(dilation):
            """Dilated conv layer with Gated+ELU activ and skip connections"""
            def f(input_):
                # Dropout of inputs
                drop = Dropout(dropout)(input_)
                # Normal activation
                normal_out = Conv1D(
                    kernels, 
                    kernel_size, 
                    padding='causal', 
                    dilation_rate = dilation ,
                    activation='tanh'
                )(drop)
                
                # Gate
                gate_out = Conv1D(
                    kernels, 
                    kernel_size, 
                    padding='causal', 
                    dilation_rate = dilation, 
                    activation='sigmoid'
                )(drop)
                # Point-wise nonlinear · gate
                merged = multiply([normal_out, gate_out])
                # Activation after gate
                skip_out = ELU()(Conv1D(kernels, 1, padding='same')(merged))
                # Residual connections: allow the network input to skip the 
                # whole block if necessary
                out = add([skip_out, input_])
                return out, skip_out
            return f
        
        def wavenetblock(maxdilation):
            """Stack of gated blocks with exponentially increasing dilations"""
            def f(input_):
                dilation = 1
                flow = input_
                skip_connections = []
                # Increasing dilation rates
                while dilation < maxdilation:
                    flow, skip = gatedblock(dilation)(flow)
                    skip_connections.append(skip)
                    dilation *= 2
                skip = add(skip_connections)
                return flow, skip
            return f
        
        input_ = Input(shape=(inputtokens, encoder.nchars))
        net = Conv1D(kernels, 1, padding='same')(input_)
        skip_connections = []
        for i in range(wavenetblocks):
            with tf.device(gpus[i % len(gpus)]):
                net, skip = wavenetblock(maxdilation)(net)
                skip_connections.append(skip)
        if wavenetblocks > 1:
            net = add(skip_connections)
        else:
            net = skip
        net = ELU()(net)
        net = Conv1D(kernels, 1)(net)
        net = ELU()(net)
        net = Conv1D(kernels, 1)(net)
        net = Flatten()(net)
        net = Dense(encoder.nchars, activation='softmax')(net)
        model = Model(inputs=input_, outputs=net)
        model.compile(optimizer=optimizer, loss='categorical_crossentropy', metrics=['accuracy'])
        return model

class LSTMModel():
    """Implementation of stacked Long-Short Term Memory model
    
    Main reference is Andrej Karpathy post on text generation with LSTMs:
        - http://karpathy.github.io/2015/05/21/rnn-effectiveness/
    """
    
    paramgrid = [
        [1,2,3], # layers
        [16,32,64,128,256,512,1024], # units
        (0.0, 1.0), # dropout
        ['sgd', 'rmsprop', 'adam'], # optimizer
    ]
    
    def create(inputtokens, encoder, layers=1, units=16, dropout=0, 
               optimizer='adam'):
        model = Sequential()
        # First LSTM layer
        if layers == 1:
            model.add(LSTM(units, activation='relu',
                      input_shape=(inputtokens, encoder.nchars)))
        else:
            model.add(LSTM(units, activation='relu', 
                      input_shape=(inputtokens, encoder.nchars),
                      return_sequences=True))
        model.add(Dropout(dropout))
        # Intermediate LSTM layers
        for i in range(1, layers-1):
            model.add(LSTM(units, activation='relu'), return_sequences=True)
            model.add(Dropout(dropout))
        # Final LSTM layer
        model.add(LSTM(units, activation='relu'))
        model.add(Dropout(dropout))
        # Output layer
        model.add(Dense(encoder.nchars, activation='softmax'))
        model.compile(optimizer=optimizer, loss='categorical_crossentropy', 
                      metrics=['accuracy'])
        return model