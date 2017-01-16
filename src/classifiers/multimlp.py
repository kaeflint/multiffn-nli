# -*- coding: utf-8 -*-

from __future__ import print_function

import json
import os
import tensorflow as tf

import utils
from trainable import Trainable, get_weights_and_biases


def attention_softmax3d(values):
    """
    Performs a softmax over the attention values.

    :param values: 3d tensor with raw values
    :return: 3d tensor, same shape as input
    """
    original_shape = tf.shape(values)
    num_units = original_shape[2]
    reshaped_values = tf.reshape(values, tf.pack([-1, num_units]))
    softmaxes = tf.nn.softmax(reshaped_values)
    return tf.reshape(softmaxes, original_shape)


def clip_sentence(sentence, sizes):
    """
    Clip the input sentence placeholders to the length of the longest one in the
    batch. This saves processing time.

    :param sentence: tensor with shape (batch, time_steps)
    :param sizes: tensor with shape (batch)
    :return: tensor with shape (batch, time_steps)
    """
    max_batch_size = tf.reduce_max(sizes)
    clipped_sent = tf.slice(sentence, [0, 0],
                            tf.pack([-1, max_batch_size]))
    return clipped_sent


def mask_values_after_sentence_end(values, sentence_sizes, value):
    """
    Given a batch of matrices, each with shape m x n, mask the values in each row
    after the positions indicated in sentence_sizes.

    :param values: tensor with shape (batch_size, m, n)
    :param sentence_sizes: tensor with shape (batch_size) containing the
        sentence sizes that should be limited
    :param value: scalar value to assign to items after sentence size
    :return: a tensor with the same shape
    """
    time_steps = tf.shape(values)[2]

    ones = tf.ones_like(values, dtype=tf.int32)
    mask = value * tf.cast(ones, tf.float32)

    # This piece of code is pretty ugly. We create a tensor with the same shape
    # as the values with each index from 0 to max_size and compare it against
    # another tensor with the same shape which holds the length of each batch.
    # We use tf.select, and then set values past sentence size to -inf.
    # If/when tensorflow had better indexing capabilities, we could simplify it.
    range_ = tf.range(time_steps)
    positions = ones * tf.reshape(range_, [1, 1, -1])
    sizes = ones * tf.reshape(sentence_sizes, [-1, 1, 1])
    cond = tf.less(positions, sizes)

    return tf.select(cond, values, mask)


class MultiFeedForwardClassifier(Trainable):
    """
    Implementation of the multi feed forward network model described in
    the paper "A Decomposable Attention Model for Natural Language
    Inference" by Parikh et al., 2016.

    It applies feedforward MLPs to combinations of parts of the two sentences,
    without any recurrent structure.
    """
    def __init__(self, num_units, num_classes,
                 vocab_size, embedding_size, use_intra_attention=False,
                 training=True, project_input=True, optimizer='adagrad',
                 distance_biases=10):

        self.num_units = num_units
        self.num_classes = num_classes
        self.use_intra = use_intra_attention
        self.project_input = project_input
        self.distance_biases = distance_biases

        # we have to supply the vocab size to allow validate_shape on the
        # embeddings variable, which is necessary down in the graph to determine
        # the shape of inputs at graph construction time
        self.embeddings_ph = tf.placeholder(tf.float32, (vocab_size, embedding_size),
                                            'embeddings')
        # sentence plaholders have shape (batch, time_steps)
        self.sentence1 = tf.placeholder(tf.int32, (None, None), 'sentence1')
        self.sentence2 = tf.placeholder(tf.int32, (None, None), 'sentence2')
        self.sentence1_size = tf.placeholder(tf.int32, [None], 'sent1_size')
        self.sentence2_size = tf.placeholder(tf.int32, [None], 'sent2_size')
        self.label = tf.placeholder(tf.int32, [None], 'label')
        self.learning_rate = tf.placeholder(tf.float32, [], name='learning_rate')
        self.l2_constant = tf.placeholder(tf.float32, [], 'l2_constant')
        self.clip_value = tf.placeholder(tf.float32, [], 'clip_norm')
        self.dropout_keep = tf.placeholder(tf.float32, None, 'dropout')
        self.embedding_size = embedding_size
        self._extra_init()

        # we initialize the embeddings from a placeholder to circumvent
        # tensorflow's limitation of 2 GB nodes in the graph
        self.embeddings = tf.Variable(self.embeddings_ph, trainable=True,
                                      validate_shape=True)

        # clip the sentences to the length of the longest one in the batch
        # this saves processing time
        clipped_sent1 = clip_sentence(self.sentence1, self.sentence1_size)
        clipped_sent2 = clip_sentence(self.sentence2, self.sentence2_size)
        embedded1 = tf.nn.embedding_lookup(self.embeddings, clipped_sent1)
        embedded2 = tf.nn.embedding_lookup(self.embeddings, clipped_sent2)

        if project_input:
            projected1 = self.project_embeddings(embedded1)
            projected2 = self.project_embeddings(embedded2, True)
            self.representation_size = self.num_units
        else:
            projected1 = embedded1
            projected2 = embedded2
            self.representation_size = self.embedding_size

        if use_intra_attention:
            # here, repr's have shape (batch , time_steps, 2*num_units)
            repr1 = self.compute_intra_attention(projected1)
            repr2 = self.compute_intra_attention(projected2, True)
            self.representation_size *= 2
        else:
            # in this case, repr's have shape (batch, time_steps, num_units)
            repr1 = projected1
            repr2 = projected2

        # the architecture has 3 main steps: soft align, compare and aggregate
        # alpha and beta have shape (batch, time_steps, embeddings)
        self.alpha, self.beta = self.attend(repr1, repr2)
        self.v1 = self.compare(repr1, self.beta, self.sentence1_size)
        self.v2 = self.compare(repr2, self.alpha, self.sentence2_size, True)
        self.logits = self.aggregate(self.v1, self.v2)
        self.answer = tf.argmax(self.logits, 1, 'answer')

        hits = tf.equal(tf.cast(self.answer, tf.int32), self.label)
        self.accuracy = tf.reduce_mean(tf.cast(hits, tf.float32),
                                       name='accuracy')
        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(self.logits,
                                                                       self.label)
        self.labeled_loss = tf.reduce_mean(cross_entropy)
        weights = [v for v in tf.trainable_variables()
                   if 'weight' in v.name]
        l2_partial_sum = sum([tf.nn.l2_loss(weight) for weight in weights])
        l2_loss = tf.mul(self.l2_constant, l2_partial_sum, 'l2_loss')
        self.loss = tf.add(self.labeled_loss, l2_loss, 'loss')

        if training:
            self._create_training_tensors(optimizer_algorithm=optimizer)
            self.merged_summaries = tf.merge_all_summaries()

    def _extra_init(self):
        """
        Entry point for subclasses initialize more stuff
        """
        pass

    def project_embeddings(self, embeddings, reuse_weights=False):
        """
        Project word embeddings into another dimensionality

        :param embeddings: embedded sentence, shape (batch, time_steps, embedding_size)
        :param reuse_weights: reuse weights in internal layers
        :return: projected embeddings with shape (batch, time_steps, num_units)
        """
        time_steps = tf.shape(embeddings)[1]
        embeddings_2d = tf.reshape(embeddings, [-1, self.embedding_size])

        with tf.variable_scope('projection', reuse=reuse_weights):
            initializer = tf.random_normal_initializer(0.0, 0.1)
            weights = tf.get_variable('weights', [self.embedding_size, self.num_units],
                                      initializer=initializer)

            projected = tf.matmul(embeddings_2d, weights)

        projected_3d = tf.reshape(projected, tf.pack([-1, time_steps, self.num_units]))
        return projected_3d

    def _relu_layer(self, inputs, weights, bias):
        """
        Apply dropout to the inputs, followed by the weights and bias,
        and finally the relu activation
        :param inputs: 2d tensor
        :param weights: 2d tensor
        :param bias: 1d tensor
        :return: 2d tensor
        """
        after_dropout = tf.nn.dropout(inputs, self.dropout_keep)
        raw_values = tf.nn.xw_plus_b(after_dropout, weights, bias)
        return tf.nn.relu(raw_values)

    def _apply_feedforward(self, inputs, num_input_units, scope,
                           reuse_weights=False, one_layer=False):
        """
        Apply two feed forward layers with self.num_units on the inputs.

        :param inputs: tensor in shape (batch, time_steps, num_input_units)
            or (batch, num_units)
        :param num_input_units: a python int
        :return: a tensor with shape (batch, time_steps, num_units)
        """
        rank = len(inputs.get_shape())
        if rank == 3:
            time_steps = tf.shape(inputs)[1]

            # combine batch and time steps in the first dimension
            inputs2d = tf.reshape(inputs, tf.pack([-1, num_input_units]))
        else:
            inputs2d = inputs

        scope = scope or 'feedforward'
        with tf.variable_scope(scope, reuse=reuse_weights):
            initializer = tf.random_normal_initializer(0.0, 0.1)

            with tf.variable_scope('layer1'):
                shape = [num_input_units, self.num_units]
                weights1 = tf.get_variable('weights', shape,
                                           initializer=initializer)
                zero_init = tf.zeros_initializer([self.num_units])
                bias1 = tf.get_variable('bias', dtype=tf.float32,
                                        initializer=zero_init)

            # relus are (time_steps * batch, num_units)
            relus = self._relu_layer(inputs2d, weights1, bias1)

            if not one_layer:
                with tf.variable_scope('layer2'):
                    shape = [self.num_units, self.num_units]
                    weights2 = tf.get_variable('weights', shape,
                                               initializer=initializer)
                    bias2 = tf.get_variable('bias', dtype=tf.float32,
                                            initializer=zero_init)
                    relus = self._relu_layer(relus, weights2, bias2)

        if rank == 3:
            output_shape = tf.pack([-1, time_steps, self.num_units])
            return tf.reshape(relus, output_shape)

        return relus

    def _get_distance_biases(self, time_steps, reuse_weights=False):
        """
        Return a 2-d tensor with the values of the distance biases to be applied
        on the intra-attention matrix of size sentence_size
        :param time_steps: tensor scalar
        :return: 2-d tensor (time_steps, time_steps)
        """
        with tf.variable_scope('distance-bias', reuse=reuse_weights):
            # this is d_{i-j}
            distance_bias = tf.get_variable('dist_bias', [self.distance_biases],
                                            initializer=tf.zeros_initializer)

            # messy tensor manipulation for indexing the biases
            r = tf.range(0, time_steps)
            r_matrix = tf.tile(tf.reshape(r, [1, -1]), tf.pack([time_steps, 1]))
            raw_inds = r_matrix - tf.reshape(r, [-1, 1])
            clipped_inds = tf.clip_by_value(raw_inds, 0, self.distance_biases - 1)
            values = tf.nn.embedding_lookup(distance_bias, clipped_inds)

        return values

    def compute_intra_attention(self, sentence, reuse_weights=False):
        """
        Compute the intra attention of a sentence. It returns a concatenation
        of the original sentence with its attended output.

        :param sentence: tensor in shape (batch, time_steps, num_units)
        :return: a tensor in shape (batch, time_steps, 2*num_units)
        """
        time_steps = tf.shape(sentence)[1]
        with tf.variable_scope('intra-attention') as scope:
            # this is F_intra in the paper
            # f_intra1 is (batch, time_steps, num_units) and
            # f_intra1_t is (batch, num_units, time_steps)
            f_intra = self._apply_feedforward(sentence, self.num_units,
                                              scope,
                                              reuse_weights=reuse_weights)
            f_intra_t = tf.transpose(f_intra, [0, 2, 1])

            # these are f_ij
            # raw_attentions is (batch, time_steps, time_steps)
            raw_attentions = tf.batch_matmul(f_intra, f_intra_t)

            # bias has shape (time_steps, time_steps)
            bias = self._get_distance_biases(time_steps, reuse_weights=reuse_weights)

            # bias is broadcast along batches
            raw_attentions += bias
            attentions = attention_softmax3d(raw_attentions)

            attended = tf.batch_matmul(attentions, sentence)

        return tf.concat(2, [sentence, attended])

    def attend(self, sent1, sent2):
        """
        Compute inter-sentence attention. This is step 1 (attend) in the paper

        :param sent1: tensor in shape (batch, time_steps, num_units),
            the projected sentence 1
        :param sent2: tensor in shape (batch, time_steps, num_units)
        :return: a tuple of 3-d tensors, alfa and beta.
        """
        with tf.variable_scope('inter-attention') as self.attend_scope:
            # this is F in the paper
            num_units = self.representation_size

            # repr1 has shape (batch, time_steps, num_units)
            # repr2 has shape (batch, num_units, time_steps)
            repr1 = self._transformation_attend(sent1, num_units,
                                                self.sentence1_size)
            repr2 = self._transformation_attend(sent2, num_units,
                                                self.sentence2_size, True)
            repr2 = tf.transpose(repr2, [0, 2, 1])

            # compute the unnormalized attention for all word pairs
            # raw_attentions has shape (batch, time_steps1, time_steps2)
            self.raw_attentions = tf.batch_matmul(repr1, repr2)

            # now get the attention softmaxes
            att_sent1 = attention_softmax3d(self.raw_attentions)

            att_transposed = tf.transpose(self.raw_attentions, [0, 2, 1])
            att_sent2 = attention_softmax3d(att_transposed)

            self.inter_att1 = att_sent1
            self.inter_att2 = att_sent2
            alpha = tf.batch_matmul(att_sent2, sent1, name='alpha')
            beta = tf.batch_matmul(att_sent1, sent2, name='beta')

        return alpha, beta

    def _transformation_attend(self, sentence, num_units, length,
                               reuse_weights=False):
        """
        Apply the transformation on each sentence before attending over each
        other. In the original model, it is a two layer feed forward network.
        :param sentence: a tensor with shape (batch, time_steps, num_units)
        :param num_units: a python int indicating the third dimension of sentence
        :param length: real length of the sentence. Not used in this class.
        :param reuse_weights: whether to reuse weights inside this scope
        :return: a tensor with shape (batch, time_steps, num_units)
        """
        return self._apply_feedforward(sentence, num_units, self.attend_scope,
                                       reuse_weights)

    def compare(self, sentence, soft_alignment, sentence_length,
                reuse_weights=False):
        """
        Apply a feed forward network to compare one sentence to its
        soft alignment with the other.

        :param sentence: embedded and projected sentence,
            shape (batch, time_steps, num_units)
        :param soft_alignment: tensor with shape (batch, time_steps, num_units)
        :param reuse_weights: whether to reuse weights in the internal layers
        :return: a tensor (batch, time_steps, num_units)
        """
        with tf.variable_scope('comparison', reuse=reuse_weights) \
                as self.compare_scope:
            num_units = 2 * self.representation_size

            # sent_and_alignment has shape (batch, time_steps, num_units)
            sent_and_alignment = tf.concat(2, [sentence, soft_alignment])

            output = self._transformation_compare(sent_and_alignment, num_units,
                                                  sentence_length, reuse_weights)

        return output

    def aggregate(self, v1, v2):
        """
        Aggregate the representations induced from both sentences and their
        representations
        :param v1: tensor with shape (batch, time_steps, num_units)
        :param v2: tensor with shape (batch, time_steps, num_units)
        :return: logits over classes, shape (batch, num_classes)
        """
        # sum over time steps; resulting shape is (batch, num_units)
        v1_sum = tf.reduce_sum(v1, [1])
        v2_sum = tf.reduce_sum(v2, [1])

        # also take the max
        v1_max = tf.reduce_max(v1, [1])
        v2_max = tf.reduce_max(v2, [1])
        concat_v = tf.concat(1, [v1_sum, v2_sum, v1_max, v2_max])

        with tf.variable_scope('aggregation') as self.aggregate_scope:
            initializer = tf.random_normal_initializer(0.0, 0.1)
            with tf.variable_scope('linear'):
                shape = [self.num_units, self.num_classes]
                weights_linear = tf.get_variable('weights', shape,
                                                 initializer=initializer)
                bias_linear = tf.get_variable('bias', [self.num_classes],
                                              initializer=tf.zeros_initializer)

            num_units = self._num_units_on_aggregate()
            pre_logits = self._apply_feedforward(concat_v, num_units,
                                                 self.aggregate_scope,
                                                 one_layer=True)
            logits = tf.nn.xw_plus_b(pre_logits, weights_linear, bias_linear)

        return logits

    def _num_units_on_aggregate(self):
        """
        Return the number of units used by the network when computing
        the aggregated representation of the two sentences.
        """
        return 4 * self.num_units

    def _transformation_compare(self, sentence, num_units, length,
                                reuse_weights=False):
        """
        Apply the transformation on each attended token before comparing.
        In the original model, it is a two layer feed forward network.

        :param sentence: a tensor with shape (batch, time_steps, num_units)
        :param num_units: a python int indicating the third dimension of sentence
        :param length: real length of the sentence. Not used in this class.
        :param reuse_weights: whether to reuse weights inside this scope
        :return: a tensor with shape (batch, time_steps, num_units)
        """
        return self._apply_feedforward(sentence, num_units, self.compare_scope,
                                       reuse_weights)

    def _create_training_tensors(self, optimizer_algorithm):
        """
        Create the tensors used for training
        """
        with tf.name_scope('training'):
            if optimizer_algorithm == 'adagrad':
                optimizer = tf.train.AdagradOptimizer(self.learning_rate)
            elif optimizer_algorithm == 'adam':
                optimizer = tf.train.AdamOptimizer(self.learning_rate)
            elif optimizer_algorithm == 'adadelta':
                optimizer = tf.train.AdadeltaOptimizer(self.learning_rate)
            else:
                ValueError('Unknown optimizer: %s' % optimizer_algorithm)

            gradients, v = zip(*optimizer.compute_gradients(self.loss))
            if self.clip_value is not None:
                gradients, _ = tf.clip_by_global_norm(gradients, self.clip_value)
            self.train_op = optimizer.apply_gradients(zip(gradients, v))

    def initialize_embeddings(self, session, embeddings):
        """
        Initialize word embeddings
        :param session: tensorflow session
        :param embeddings: the contents of the word embeddings
        :return:
        """
        init_op = tf.variables_initializer([self.embeddings])
        session.run(init_op, {self.embeddings_ph: embeddings})

    def initialize(self, session, embeddings):
        """
        Initialize all tensorflow variables.
        :param session: tensorflow session
        :param embeddings: the contents of the word embeddings
        """
        init_op = tf.global_variables_initializer()
        session.run(init_op, {self.embeddings_ph: embeddings})

    @classmethod
    def _init_from_load(cls, params, training):
        """
        Call the constructor inside the loader
        :return: an instance of this class
        """
        return cls(params['num_units'], params['num_classes'],
                   params['vocab_size'], params['embedding_size'],
                   project_input=params['project_input'], training=training)

    @classmethod
    def load(cls, dirname, session, training=False):
        """
        Load a previously saved file.
        :param dirname: directory with model files
        :param session: tensorflow session
        :param training: whether to create training tensors
        :return: an instance of MultiFeedForward
        """
        params = utils.load_parameters(dirname)
        model = cls._init_from_load(params, training)

        tensorflow_file = os.path.join(dirname, 'model')
        saver = tf.train.Saver(get_weights_and_biases())
        saver.restore(session, tensorflow_file)

        # if training, optimizer values still have to be initialized
        if training:
            train_vars = [v for v in tf.global_variables()
                          if v.name.startswith('training')]
            init_op = tf.variables_initializer(train_vars)
            session.run(init_op)

        return model

    def _get_params_to_save(self):
        """
        Return a dictionary with data for reconstructing a persisted object
        """
        vocab_size = self.embeddings.get_shape()[0].value
        data = {'num_units': self.num_units,
                'num_classes': self.num_classes,
                'vocab_size': vocab_size,
                'embedding_size': self.embedding_size,
                'project_input': self.project_input}

        return data

    def save(self, dirname, session, saver):
        """
        Persist a model's information
        """
        params = self._get_params_to_save()
        tensorflow_file = os.path.join(dirname, 'model')
        params_file = os.path.join(dirname, 'model-params.json')

        with open(params_file, 'wb') as f:
            json.dump(params, f)

        saver.save(session, tensorflow_file)

    def _create_batch_feed(self, sentence1, sentence2, size1, size2,
                           label, learning_rate, dropout_keep, l2, clip_value):
        feeds = {self.sentence1: sentence1,
                 self.sentence2: sentence2,
                 self.sentence1_size: size1,
                 self.sentence2_size: size2,
                 self.label: label,
                 self.learning_rate: learning_rate,
                 self.dropout_keep: dropout_keep,
                 self.l2_constant: l2,
                 self.clip_value: clip_value
                 }
        return feeds

    def _run_on_validation(self, session, feeds):
        loss, acc = session.run([self.loss, self.accuracy], feeds)
        msg = 'Validation loss: %f\tValidation accuracy: %f' % (loss, acc)
        return loss, msg

    def evaluate(self, session, dataset, return_answers):
        """
        Run the model on the given dataset

        :param session: tensorflow session
        :param dataset: the dataset object
        :type dataset: utils.RTEDataset
        :param return_answers: if True, also return the answers
            the system gave
        :return: if not return_answers, a tuple (loss, accuracy)
            or else (loss, accuracy, answers)
        """
        assert isinstance(dataset, utils.RTEDataset)
        feeds = self._create_batch_feed(dataset.sentences1,
                                        dataset.sentences2,
                                        dataset.sizes1,
                                        dataset.sizes2,
                                        dataset.labels,
                                        0, 1, 0, 0)
        if return_answers:
            ops = [self.loss, self.accuracy, self.answer]
        else:
            ops = [self.loss, self.accuracy]
        return session.run(ops, feeds)

    def train(self, session, train_dataset, valid_dataset, save_dir,
              learning_rate, num_epochs, batch_size, dropout_keep=1, l2=0,
              clip_norm=10, report_interval=100):
        """
        Train the model with the specified parameters
        :param session: tensorflow session
        :param train_dataset: an RTEDataset object with training data
        :param valid_dataset: an RTEDataset object with validation data
        :param save_dir: path to directory to save the model
        :param learning_rate: the learning rate
        :param num_epochs: number of epochs to run the model. During each epoch,
            all data points are seen exactly once
        :param batch_size: how many items in each minibatch.
        :param dropout_keep: dropout keep probability (applied at network
            input and output)
        :param l2: l2 loss constant
        :param clip_norm: global tensor norm to clip
        :param report_interval: how many minibatches between each performance report
        :return:
        """
        train = super(MultiFeedForwardClassifier, self)._train
        train(session, get_weights_and_biases(), save_dir, train_dataset,
              valid_dataset, learning_rate, num_epochs, batch_size,
              dropout_keep, l2, clip_norm, report_interval)
