#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: Aneesh Joshi <aneeshyjoshi@gmail.com>

"""This module makes a trainable and usable model for getting similarity between documents using the DRMM_TKS model.

Once the model is trained with the query-candidate-relevance data, the model can provide a vector for each new
document which is entered into it. The similarity between any 2 documents can then be measured using the
cosine similarty between the vectors.

Abbreviations
=============
- DRMM : Deep Relevance Matching Model
- TKS : Top K Solutions

About DRMM_TKS
==============
This is a variant version of DRMM, which applied topk pooling in the matching matrix.
It has the following steps:

1. embed queries and docs into embedding vector named `q_embed` and `d_embed` respectively.
2. computing `q_embed` and `d_embed` with element-wise multiplication.
3. computing output of upper layer with dense layer operation.
4. take softmax operation on the output of this layer named `g` and find the k largest entries named `mm_k`.
5. input `mm_k` into hidden layers, with specified length of layers and activation function.
6. compute `g` and `mm_k` with element-wise multiplication.


This class has 3 modes:
1. Ranking
2. Classification
3. Inference

Ranking:
For a query and a group of candidate documents, rank them relative to each other
Example dataset : WikiQA, InsuranceQA

Classification:
Basically just paraphrase detection. Is sentenceA and sentenceB the same semantically?
Example dataset : Quora Duplicate Questions

Inference:
Given 2 sentences, we try to predict the relation between them.
"Bob likes Alice", "Bob hates Alice" --> Contradiction


On predicting, the model returns the score list between queries and documents.

The trained model needs to be trained on data in the format:

>>> from gensim.models.experimental import DRMM_TKS
>>> import gensim.downloader as api
>>> queries = ["When was World War 1 fought ?".lower().split(), "When was Gandhi born ?".lower().split()]
>>> docs = [["The world war was bad".lower().split(), "It was fought in 1996".lower().split()], ["Gandhi was born in"
...        "the 18th century".lower().split(), "He fought for the Indian freedom movement".lower().split(),
...        "Gandhi was assasinated".lower().split()]]
>>> labels = [[0, 1], [1, 0, 0]]
>>> word_embeddings_kv = api.load('glove-wiki-gigaword-50')
>>> model = DRMM_TKS(queries, docs, labels, word_embedding=word_embeddings_kv, verbose=0)

Persist a model to disk with :

>>> from gensim.test.utils import get_tmpfile
>>> file_path = get_tmpfile('DRMM_TKS.model')
>>> model.save(file_path)
>>> model = DRMM_TKS.load(file_path)

You can also create the modela and train it later :

>>> model = DRMM_TKS()
>>> model.train(queries, docs, labels, word_embeddings_kv, epochs=12, verbose=0)

Testing on new data :

>>> from gensim.test.utils import datapath
>>> model = DRMM_TKS.load(datapath('drmm_tks'))
>>>
>>> queries = ["how are glacier caves formed ?".lower().split()]
>>> docs = [["A partly submerged glacier cave on Perito Moreno Glacier".lower().split(), "glacier cave is cave formed"
...        " within the ice of glacier".lower().split()]]
>>> print(model.predict(queries, docs))
[[0.9915068 ]
 [0.99228466]]
>>> print(model.predict([["hello", "world"]], [[["i", "am", "happy"], ["good", "morning"]]]))
[[0.9975487]
 [0.999115 ]]


More information can be found in:
`Jiafeng Guo, Yixing Fan, Qingyao Ai, W. Bruce Croft "A Deep Relevance Matching Model for Ad-hoc Retrieval"
<http://www.bigdatalab.ac.cn/~gjf/papers/2016/CIKM2016a_guo.pdf>`_
`MatchZoo Repository <https://github.com/faneshion/MatchZoo>`_
`Similarity Learning Wikipedia Page <https://en.wikipedia.org/wiki/Similarity_learning>`_

"""

import logging
import numpy as np
import random as rn
import tensorflow as tf
import hashlib
from numpy import random as np_random
from gensim.models import KeyedVectors
from collections import Counter
from .utils.custom_losses import rank_hinge_loss
from .utils.custom_layers import TopKLayer
from .utils.custom_callbacks import ValidationCallback
from .utils.evaluation_metrics import mapk, mean_ndcg
from sklearn.preprocessing import normalize
from gensim import utils
from collections import Iterable
from keras.utils.np_utils import to_categorical

import keras.backend as K
from keras import optimizers
from keras.models import load_model
from keras.losses import hinge
from keras.models import Model
from keras.layers import Input, Embedding, Dot, Dense, Reshape, Dropout

# Set random seed for reproducible results.
# For more details, read the keras docs:
# https://keras.io/getting-started/faq/#how-can-i-obtain-reproducible-results-using-keras-during-development
import os
os.environ['PYTHONHASHSEED'] = '0'
# The below is necessary for starting Numpy generated random numbers
# in a well-defined initial state.
np.random.seed(42)
# The below is necessary for starting core Python generated random numbers
# in a well-defined state.
rn.seed(12345)
# Force TensorFlow to use single thread.
# Multiple threads are a potential source of
# non-reproducible results.
# For further details, see: https://stackoverflow.com/questions/42022950/which-seeds-have-to-be-set-where-to-realize-100-reproducibility-of-training-res
session_conf = tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=1)
from keras import backend as K
# The below tf.set_random_seed() will make random number generation
# in the TensorFlow backend have a well-defined initial state.
# For further details, see: https://www.tensorflow.org/api_docs/python/tf/set_random_seed
tf.set_random_seed(1234)
sess = tf.Session(graph=tf.get_default_graph(), config=session_conf)
K.set_session(sess)

logger = logging.getLogger(__name__)


def _get_full_batch_iter(pair_list, batch_size):
    """Provides all the data points int the format: X1, X2, y with
    alternate positive and negative examples of `batch_size` in a streamable format.

    Parameters
    ----------
    pair_list : iterable list of tuple
                See docstring for _get_pair_list for more details
    batch_size : int
        half the size in which the generator will yield datapoints. The size is doubled since
        we include positive and negative examples.

    Yields
    -------
    X1 : numpy array of shape (batch_size * 2, text_maxlen)
        the queries
    X2 : numpy array of shape (batch_size * 2, text_maxlen)
        the docs
    y : numpy array with {0, 1} of shape (batch_size * 2, 1)
        The relation between X1[i] and X2[j]
        1 : X2[i] is relevant to X1[i]
        0 : X2[i] is not relevant to X1[i]
    """

    X1, X2, y = [], [], []
    while True:
        j=0
        for i, (query, pos_doc, neg_doc) in enumerate(pair_list):
            X1.append(query)
            X2.append(pos_doc)
            y.append(1)
            X1.append(query)
            X2.append(neg_doc)
            y.append(0)
            j+=1
            if i % batch_size == 0 and i != 0:
                yield ({'query': np.array(X1), 'doc': np.array(X2)}, np.array(y))
                X1, X2, y = [], [], []


def _get_pair_list(queries, docs, labels, _make_indexed):
    """Yields a tuple with query document pairs in the format
    (query, positive_doc, negative_doc)
    [(q1, d+, d-), (q2, d+, d-), (q3, d+, d-), ..., (qn, d+, d-)]
        where each query or document is a list of int

    Parameters
    ----------
    queries : iterable list of list of str
        The queries to the model
    docs : iterable list of list of list of str
        The candidate documents for each query
    labels : iterable list of int
        The relevance of the document to the query. 1 = relevant, 0 = not relevant
    _make_indexed : function
        Translates the given sentence as a list of list of str into a list of list of int
        based on the model's internal dictionary

    Example
    -------
    [(['When', 'was', 'Abraham', 'Lincoln', 'born', '?'],
      ['He', 'was', 'born', 'in', '1809'],
      ['Abraham', 'Lincoln', 'was', 'the', 'president',
       'of', 'the', 'United', 'States', 'of', 'America']),

     (['When', 'was', 'the', 'first', 'World', 'War', '?'],
      ['It', 'was', 'fought', 'in', '1914'],
      ['There', 'were', 'over', 'a', 'million', 'deaths']),

     (['When', 'was', 'the', 'first', 'World', 'War', '?'],
      ['It', 'was', 'fought', 'in', '1914'],
      ['The', 'first', 'world', 'war', 'was', 'bad'])
    ]

    """
    while True:
        j=0
        for q, doc, label in zip(queries, docs, labels):
            doc, label = (list(t) for t in zip(*sorted(zip(doc, label), reverse=True)))
            for item in zip(doc, label):
                if item[1] == 1:
                    for new_item in zip(doc, label):
                        if new_item[1] == 0:
                            j+=1
                            yield(_make_indexed(q), _make_indexed(item[0]), _make_indexed(new_item[0]))


class DRMM_TKS(utils.SaveLoad):
    """Model for training a Similarity Learning Model using the DRMM TKS model.
    You only have to provide sentences in the data as a list of words.
    """

    def __init__(self, queries=None, docs=None, labels=None, word_embedding=None,
                 text_maxlen=200, normalize_embeddings=True, epochs=10, unk_handle_method='random',
                 validation_data=None, topk=50, target_mode='ranking', verbose=1, batch_size=20, steps_per_epoch=20,
                 num_inferences=3):
        """Initializes the model and trains it

        Parameters
        ----------
        queries: iterable list of list of string words, optional
            The questions for the similarity learning model.
        docs: iterable list of list of list of string words, optional
            The candidate answers for the similarity learning model.
        labels: iterable list of list of int, optional
            Indicates when a candidate document is relevant to a query
            - 1 : relevant
            - 0 : irrelevant
        word_embedding : :class:`~gensim.models.keyedvectors.KeyedVectors`, optional
            a KeyedVector object which has the embeddings pre-loaded.
            If None, random word embeddings will be used.
        text_maxlen : int, optional
            The maximum possible length of a query or a document.
            This is used for padding sentences.
        normalize_embeddings : bool, optional
            Whether the word embeddings provided should be normalized.
        epochs : int, optional
            The number of epochs for which the model should train on the data.
        unk_handle_method : {'zero', 'random'}, optional
            The method for handling unkown words.
                - 'zero' : unknown words are given a zero vector
                - 'random' : unknown words are given a uniformly random vector bassed on the word string hash
        validation_data: list of the form [test_queries, test_docs, test_labels], optional
            where test_queries, test_docs  and test_labels are of the same form as
            their counter parts stated above.
        topk : int, optional
            the k topmost values in the interaction matrix between the queries and the docs
        target_mode : {'ranking', 'classification'}, optional
            the way the model should be trained, either to rank or classify
        verbose : {0, 1, 2}
            the level of information shared while training
                - 0 : silent
                - 1 : progress bar
                - 2 : one line per epoch


        Examples
        --------
        The trained model needs to be trained on data in the format

        >>> queries = ["When was World War 1 fought ?".lower().split(), "When was Gandhi born ?".lower().split()]
        >>> docs = [["The world war was bad".lower().split(), "It was fought in 1996".lower().split()], ["Gandhi was"
        ...    "born in the 18th century".lower().split(), "He fought for the Indian freedom movement".lower().split(),
        ...    "Gandhi was assasinated".lower().split()]]
        >>> labels = [[0, 1], [1, 0, 0]]
        >>> import gensim.downloader as api
        >>> word_embeddings_kv = api.load('glove-wiki-gigaword-50')
        >>> model = DRMM_TKS(queries, docs, labels, word_embedding=word_embeddings_kv, verbose=0)
        """
        self.queries = queries
        self.docs = docs
        self.labels = labels
        self.word_counter = Counter()
        self.text_maxlen = text_maxlen
        self.topk = topk
        self.word_embedding = word_embedding
        self.word2index, self.index2word = {}, {}
        self.normalize_embeddings = normalize_embeddings
        self.model = None
        self.epochs = epochs
        self.validation_data = validation_data
        self.target_mode = target_mode
        self.verbose = verbose
        self.first_train = True  # Whether the model has been trained before
        self.needs_vocab_build = True
        self.batch_size = batch_size
        self.steps_per_epoch = steps_per_epoch

        # These functions have been defined outside the class and set as attributes here
        # so that they can be ignored when saving the model to file
        self._get_pair_list = _get_pair_list
        self._get_full_batch_iter = _get_full_batch_iter

        if self.target_mode not in ['ranking', 'classification', 'inference']:
            raise ValueError(
                "Unkown target_mode %s. It must be either 'ranking' or 'classification' or 'inference' " % self.target_mode
            )

        if self.target_mode == 'inference':
            self.num_inferences = num_inferences
            logger.info('Inference mode with %d labels' % self.num_inferences)

        if unk_handle_method not in ['random', 'zero']:
            raise ValueError("Unkown token handling method %s" % str(unk_handle_method))
        self.unk_handle_method = unk_handle_method

        if self.queries is not None and self.docs is not None and self.labels is not None:
            self.build_vocab(self.queries, self.docs, self.labels, self.word_embedding)
            self.train(self.queries, self.docs, self.labels, self.word_embedding,
                       self.text_maxlen, self.normalize_embeddings, self.epochs, self.unk_handle_method,
                       self.validation_data, self.topk, self.target_mode, self.verbose)

    def build_vocab(self, queries, docs, labels, word_embedding):
        """Indexes all the words and makes an embedding_matrix which
        can be fed directly into an Embedding layer
        """

        logger.info("Starting Vocab Build")

        # get all the vocab words
        for q in self.queries:
            self.word_counter.update(q)

        if self.target_mode in ['classification', 'inference']:
            for doc in self.docs:
                self.word_counter.update(doc)
        else:
            for doc in self.docs:
                for d in doc:
                    self.word_counter.update(d)

        for i, word in enumerate(self.word_counter.keys()):
            self.word2index[word] = i
            self.index2word[i] = word

        self.vocab_size = len(self.word2index)
        logger.info("Vocab Build Complete")
        logger.info("Vocab Size is %d", self.vocab_size)

        logger.info("Building embedding index using KeyedVector pretrained word embeddings")
        if type(self.word_embedding) == KeyedVectors:
            kv_model = self.word_embedding
            embedding_vocab_size, self.embedding_dim = len(kv_model.vocab), kv_model.vector_size
        else:
            raise ValueError(
                    "Unknown value of word_embedding : %s. Must be either a KeyedVector object",
                    str(word_embedding)
                )

        logger.info(
            "The embeddings_index built from the given file has %d words of %d dimensions",
            embedding_vocab_size, self.embedding_dim
        )

        logger.info("Building the Embedding Matrix for the model's Embedding Layer")

        # Initialize the embedding matrix
        # UNK word gets the vector based on the method
        if self.unk_handle_method == 'random':
            self.embedding_matrix = np.random.uniform(-0.2, 0.2, (self.vocab_size, self.embedding_dim))
        elif self.unk_handle_method == 'zero':
            self.embedding_matrix = np.zeros((self.vocab_size, self.embedding_dim))

        n_non_embedding_words = 0
        for word, i in self.word2index.items():
            if word in kv_model:
                # words not found in keyed vectors will get the vector based on unk_handle_method
                self.embedding_matrix[i] = kv_model[word]
            else:
                if self.unk_handle_method == 'random':
                    # Creates the same random vector for the given string each time
                    self.embedding_matrix[i] = self._seeded_vector(word, self.embedding_dim)
                n_non_embedding_words += 1
        logger.info(
            "There are %d words out of %d (%.2f%%) not in the embeddings. Setting them to %s", n_non_embedding_words,
            self.vocab_size, n_non_embedding_words * 100 / self.vocab_size, self.unk_handle_method
        )

        # Include embeddings for words in embedding file but not in the train vocab
        # It will be useful for embedding words encountered in validation and test set
        logger.info(
            "Adding additional words from the embedding file to embedding matrix"
        )

        # The point where vocab words end
        vocab_offset = self.vocab_size
        extra_embeddings = []
        # Take the words in the embedding file which aren't there int the train vocab
        for word in list(kv_model.vocab):
            if word not in self.word2index:
                # Add the new word's vector and index it
                extra_embeddings.append(kv_model[word])
                # We also need to keep an additional indexing of these
                # words
                self.word2index[word] = vocab_offset
                vocab_offset += 1

        # Set the pad and unk word to second last and last index
        self.pad_word_index = vocab_offset
        self.unk_word_index = vocab_offset + 1

        if self.unk_handle_method == 'random':
            unk_embedding_row = np.random.uniform(-0.2, 0.2, (1, self.embedding_dim))
        elif self.unk_handle_method == 'zero':
            unk_embedding_row = np.zeros((1, self.embedding_dim))

        pad_embedding_row = np.random.uniform(-0.2,
                                              0.2, (1, self.embedding_dim))

        if len(extra_embeddings) > 0:
            self.embedding_matrix = np.vstack(
                [self.embedding_matrix, np.array(extra_embeddings),
                 pad_embedding_row, unk_embedding_row]
            )
        else:
            self.embedding_matrix = np.vstack(
                [self.embedding_matrix, pad_embedding_row, unk_embedding_row]
            )

        if self.normalize_embeddings:
            logger.info("Normalizing the word embeddings")
            self.embedding_matrix = normalize(self.embedding_matrix)

        logger.info("Embedding Matrix build complete. It now has shape %s", str(self.embedding_matrix.shape))
        logger.info("Pad word has been set to index %d", self.pad_word_index)
        logger.info("Unknown word has been set to index %d", self.unk_word_index)
        logger.info("Embedding index build complete")
        self.needs_vocab_build = False

    def _string2numeric_hash(self, text):
        "Gets a numeric hash for a given string"
        return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)

    def _seeded_vector(self, seed_string, vector_size):
        """Create one 'random' vector (but deterministic by seed_string)"""
        # Note: built-in hash() may vary by Python version or even (in Py3.x) per launch
        once = np_random.RandomState(self._string2numeric_hash(seed_string) & 0xffffffff)
        return (once.rand(vector_size) - 0.5) / vector_size

    def _make_indexed(self, sentence):
        """Gets the indexed version of the sentence based on the self.word2index dict
        in the form of a list

        This function should never encounter any OOV words since it only indexes
        in vocab words

        Parameters
        ----------
        sentence : iterable list of list of str
            The sentence to be indexed

        Raises
        ------
        ValueError : If the sentence has a lenght more than text_maxlen
        """

        indexed_sent = []
        for word in sentence:
            if word in self.word2index:
                indexed_sent.append(self.word2index[word])
            else:
                indexed_sent.append(self.unk_word_index)

        if len(indexed_sent) > self.text_maxlen:
            indexed_sent = indexed_sent[:self.text_maxlen]
            print(
                "text_maxlen: %d isn't big enough. Error at sentence of length %d."
                "Sentence is %s" % (self.text_maxlen, len(sentence), sentence)
            )
        indexed_sent = indexed_sent + [self.pad_word_index] * (self.text_maxlen - len(indexed_sent))
        return indexed_sent

    def _get_full_batch(self):
        """Provides all the data points int the format: X1, X2, y with
        alternate positive and negative examples

        Returns
        -------
        X1 : numpy array of shape (num_samples, text_maxlen)
            the queries
        X2 : numpy array of shape (num_samples, text_maxlen)
            the docs
        y : numpy array with {0, 1} of shape (num_samples,)
            The relation between X1[i] and X2[j]
            1 : X2[i] is relevant to X1[i]
            0 : X2[i] is not relevant to X1[i]
        """
        X1, X2, y = [], [], []
        for i, (query, pos_doc, neg_doc) in enumerate(self.pair_list):
            X1.append(query)
            X2.append(pos_doc)
            y.append(1)
            X1.append(query)
            X2.append(neg_doc)
            y.append(0)

        print('There are pairs in pair_list', np.array(X1).shape, np.array(X2).shape, np.array(y).shape)
        return np.array(X1), np.array(X2), np.array(y)

    def _get_classification_batch(self, batch_size):
        """Yields batches of data to train for classification tasks"""
        while True:
            x1_batch, x2_batch, dupl_batch = [], [], []
            for x1, x2, d in zip(self.queries, self.docs, self.labels):
                x1_batch.append(self._make_indexed(x1))
                x2_batch.append(self._make_indexed(x2))
                dupl_batch.append(to_categorical(d, 2))

                if len(x1_batch) % batch_size == 0:
                    yield ({'query': np.array(x1_batch), 'doc': np.array(x2_batch)}, np.squeeze(np.array(dupl_batch)))
                    x1_batch, x2_batch, dupl_batch = [], [], []

    def _get_inference_batch(self, batch_size):
        """Yields batches of data to train for inference tasks"""
        while True:
            x1_batch, x2_batch, dupl_batch = [], [], []
            for x1, x2, d in zip(self.queries, self.docs, self.labels):
                x1_batch.append(self._make_indexed(x1))
                x2_batch.append(self._make_indexed(x2))
                dupl_batch.append(to_categorical(d, self.num_inferences))

                if len(x1_batch) % batch_size == 0:
                    yield ({'query': np.array(x1_batch), 'doc': np.array(x2_batch)}, np.squeeze(np.array(dupl_batch)))
                    x1_batch, x2_batch, dupl_batch = [], [], []

    def train(self, queries, docs, labels, word_embedding=None,
              text_maxlen=200, normalize_embeddings=True, epochs=10, unk_handle_method='zero',
              validation_data=None, topk=20, target_mode='ranking', verbose=1, batch_size=5, steps_per_epoch=900):
        """Trains a DRMM_TKS model using specified parameters

        This method is called from on model initialization if the data is provided.
        It can also be trained in an online manner or after initialization
        """

        self.queries = queries or self.queries
        self.docs = docs or self.docs
        self.labels = labels or self.labels

        # This won't change the embedding layer TODO
        self.word_embedding = word_embedding or self.word_embedding
        self.text_maxlen = text_maxlen or self.text_maxlen
        self.normalize_embeddings = normalize_embeddings or self.normalize_embeddings
        self.epochs = epochs or self.epochs
        self.unk_handle_method = unk_handle_method or self.unk_handle_method
        self.validation_data = validation_data or self.validation_data
        self.topk = topk or self.topk
        self.target_mode = target_mode or self.target_mode

        if verbose != 0:  # Check needed since 0 or 2 will always give 2
            self.verbose = verbose or self.verbose
        else:
            self.verbose = 0

        if self.queries is None or self.docs is None or self.labels is None:
            raise ValueError("queries, docs and labels have to be specified")
        # We need to build these each time since any of the parameters can change from each train to trian
        if self.needs_vocab_build:
            self.build_vocab(self.queries, self.docs, self.labels, self.word_embedding)

        if self.target_mode == 'ranking':
            self.pair_list = self._get_pair_list(self.queries, self.docs, self.labels, self._make_indexed)    
            train_generator = self._get_full_batch_iter(self.pair_list, self.batch_size)
        elif self.target_mode == 'classification':
            train_generator = self._get_classification_batch(self.batch_size)
        elif self.target_mode == 'inference':
            train_generator = self._get_inference_batch(self.batch_size)
        else:
            ValueError('Unkown target mode %s' % str(self.target_mode))


        if self.first_train:
            # The settings below should be set only once
            self.model = self._get_keras_model()
            optimizer = 'adadelta'
            optimizer = optimizers.get(optimizer)
            learning_rate = 0.0001
            learning_rate = 1
            K.set_value(optimizer.lr, learning_rate)
            loss = rank_hinge_loss
            if self.target_mode == 'classification':
                loss = 'categorical_crossentropy'
                optimizer = 'adam'
            elif self.target_mode == 'inference':
                loss = 'categorical_crossentropy'
                optimizer = 'adam'
            self.model.compile(optimizer=optimizer, loss=loss, metrics=['accuracy'])
        else:
            logger.info("Model will be retrained")

        self.model.summary(print_fn=logger.info)

        # Put the validation data in as a callback
        val_callback = None
        if self.validation_data is not None:
            test_queries, test_docs, test_labels = self.validation_data

            long_doc_list = []
            long_label_list = []
            long_query_list = []
            doc_lens = []

            for query, doc, label in zip(test_queries, test_docs, test_labels):
                i = 0
                for d, l in zip(doc, label):
                    long_query_list.append(query)
                    long_doc_list.append(d)
                    long_label_list.append(l)
                    i += 1
                doc_lens.append(len(doc))

            indexed_long_query_list = self._translate_user_data(long_query_list)
            indexed_long_doc_list = self._translate_user_data(long_doc_list)

            val_callback = ValidationCallback(
                                {"X1": indexed_long_query_list, "X2": indexed_long_doc_list, "doc_lengths": doc_lens,
                                "y": long_label_list}
                            )
            val_callback = [val_callback]  # since `model.fit` requires a list

        # If train is called again, not all values should be reset
        if self.first_train is True:
            self.first_train = False


        self.model.fit_generator(train_generator, steps_per_epoch=self.steps_per_epoch, callbacks=val_callback,
                                 epochs=self.epochs, shuffle=False)


    def _translate_user_data(self, data, silent_mode=True):
        """Translates given user data into an indexed format which the model understands.
        If a model is not in the vocabulary, it is assigned the `unk_word_index` which maps
        to the unk vector decided by `unk_handle_method`

        Parameters
        ----------
        data : list of list of string words
            The data to be tranlsated

        Examples
        --------
        >>> from gensim.test.utils import datapath
        >>> model = DRMM_TKS.load(datapath('drmm_tks'))
        >>>
        >>> queries = ["When was World War 1 fought ?".split(), "When was Gandhi born ?".split()]
        >>> print(model._translate_user_data(queries))
        [[31  1 23 31  4  5  6 30 30 30]
         [31  1 31  8  6 30 30 30 30 30]]
        """
        translated_data = []
        n_skipped_words = 0
        for sentence in data:
            translated_sentence = []
            for word in sentence:
                if word in self.word2index:
                    translated_sentence.append(self.word2index[word])
                else:
                    # If the key isn't there give it the zero word index
                    translated_sentence.append(self.unk_word_index)
                    n_skipped_words += 1
            if len(translated_sentence) > self.text_maxlen:
                '''logger.info(
                    "text_maxlen: %d isn't big enough. Error at sentence of length %d."
                    "Sentence is %s. Clipping it for now", self.text_maxlen, len(sentence), str(sentence)
                )'''
                translated_sentence = translated_sentence[:self.text_maxlen]
            translated_sentence = translated_sentence + \
                (self.text_maxlen - len(sentence)) * [self.pad_word_index]
            translated_data.append(np.array(translated_sentence))

        if not silent_mode:
            logger.info(
                "Found %d unknown words. Set them to unknown word index : %d", n_skipped_words, self.unk_word_index
            )
        return np.array(translated_data)

    def predict(self, queries, docs, silent=True):
        """Predcits the similarity between a query-document pair
        based on the trained DRMM TKS model

        Parameters
        ----------
        queries : list of list of str
            The questions for the similarity learning model
        docs : list of list of list of str
            The candidate answers for the similarity learning model


        Examples
        --------
        >>> from gensim.test.utils import datapath
        >>> model = DRMM_TKS.load(datapath('drmm_tks'))
        >>>
        >>> queries = ["When was World War 1 fought ?".split(), "When was Gandhi born ?".split()]
        >>> docs = [["The world war was bad".split(), "It was fought in 1996".split()], ["Gandhi was born in the 18th"
        ...        " century".split(), "He fought for the Indian freedom movement".split(), "Gandhi was"
        ...        " assasinated".split()]]
        >>> print(model.predict(queries, docs))
        [[0.9933108 ]
         [0.9925415 ]
         [0.9827911 ]
         [0.99258184]
         [0.9960481 ]]
        """

        long_query_list = []
        long_doc_list = []
        for query, doc in zip(queries, docs):
            for d in doc:
                long_query_list.append(query)
                long_doc_list.append(d)

        indexed_long_query_list = self._translate_user_data(long_query_list)
        indexed_long_doc_list = self._translate_user_data(long_doc_list)

        predictions = self.model.predict(x={'query': indexed_long_query_list, 'doc': indexed_long_doc_list})
        
        if not silent:
            logger.info("Predictions in the format query, doc, similarity")
            for i, (q, d) in enumerate(zip(long_query_list, long_doc_list)):
                logger.info("%s\t%s\t%s", str(q), str(d), str(predictions[i][0]))

        return predictions

    def evaluate(self, queries, docs, labels):
        """Evaluates the model and provides the results in terms of metrics (MAP, nDCG)
        This should ideally be called on the test set.

        Parameters
        ----------
        queries : list of list of str
            The questions for the similarity learning model
        docs : list of list of list of str
            The candidate answers for the similarity learning model
        labels : list of list of int
            The relevance of the document to the query. 1 = relevant, 0 = not relevant
        """
        long_doc_list = []
        long_label_list = []
        long_query_list = []
        doc_lens = []
        for query, doc, label in zip(queries, docs, labels):
            i = 0
            for d, l in zip(doc, label):
                long_query_list.append(query)
                long_doc_list.append(d)
                long_label_list.append(l)
                i += 1
            doc_lens.append(len(doc))
        indexed_long_query_list = self._translate_user_data(long_query_list)
        indexed_long_doc_list = self._translate_user_data(long_doc_list)
        predictions = self.model.predict(x={'query': indexed_long_query_list, 'doc': indexed_long_doc_list})
        Y_pred = []
        Y_true = []
        offset = 0
        for doc_size in doc_lens:
            Y_pred.append(predictions[offset: offset + doc_size])
            Y_true.append(long_label_list[offset: offset + doc_size])
            offset += doc_size
        logger.info("MAP: %.2f", mapk(Y_true, Y_pred))
        for k in [1, 3, 5, 10, 20]:
            logger.info("nDCG@%d : %.2f", k, mean_ndcg(Y_true, Y_pred, k=k))

    def save(self, fname, *args, **kwargs):
        """Save the model.
        This saved model can be loaded again using :func:`~gensim.models.experimental.drmm_tks.DRMM_TKS.load`
        The keras model shouldn't be serialized using pickle or cPickle. So, the non-keras
        variables will be saved using gensim's SaveLoad and the keras model will be saved using
        the keras save method with ".keras" prefix.

        Also see :func:`~gensim.models.experimental.drmm_tks.DRMM_TKS.load`

        Parameters
        ----------
        fname : str
            Path to the file.

        Examples
        --------
        >>> from gensim.test.utils import datapath, get_tmpfile
        >>> model = DRMM_TKS.load(datapath('drmm_tks'))
        >>> model_save_path = get_tmpfile('drmm_tks_model')
        >>> model.save(model_save_path)
        """
        # don't save the keras model as it needs to be saved with a keras function
        # Also, we can't save iterable properties. So, ignore them.
        kwargs['ignore'] = kwargs.get(
                            'ignore', ['model', '_get_pair_list', '_get_full_batch_iter',
                                        'queries', 'docs', 'labels', 'pair_list'])
        kwargs['fname_or_handle'] = fname
        super(DRMM_TKS, self).save(*args, **kwargs)
        self.model.save(fname + ".keras")

    @classmethod
    def load(cls, *args, **kwargs):
        """Loads a previously saved `DRMM TKS` model. Also see `save()`.
        Collects the gensim and the keras models and returns it as on gensim model.

        Parameters
        ----------
        fname : str
            Path to the saved file.

        Returns
        -------
        :obj: `~gensim.models.experimental.DRMM_TKS`
            Returns the loaded model as an instance of :class: `~gensim.models.experimental.DRMM_TKS`.


        Examples
        --------
        >>> from gensim.test.utils import datapath, get_tmpfile
        >>> model_file_path = datapath('drmm_tks')
        >>> model = DRMM_TKS.load(model_file_path)
        """
        fname = args[0]
        gensim_model = super(DRMM_TKS, cls).load(*args, **kwargs)
        keras_model = load_model(
            fname + '.keras', custom_objects={'TopKLayer': TopKLayer, 'rank_hinge_loss': rank_hinge_loss})
        gensim_model.model = keras_model
        gensim_model._get_pair_list = _get_pair_list
        gensim_model._get_full_batch_iter = _get_full_batch_iter
        return gensim_model

    def _get_keras_model(self, embed_trainable=False, dropout_rate=0.5, hidden_sizes=[100, 1]):
        """Builds and returns the keras class for drmm tks model

        About DRMM_TKS
        --------------
        This is a variant version of DRMM, which applied topk pooling in the matching matrix.
        It has the following steps:
        1. embed queries into embedding vector named 'q_embed' and 'd_embed' respectively
        2. computing 'q_embed' and 'd_embed' with element-wise multiplication
        3. computing output of upper layer with dense layer operation
        4. take softmax operation on the output of this layer named 'g' and find the k largest entries named 'mm_k'.
        5. input 'mm_k' into hidden layers, with specified length of layers and activation function
        6. compute 'g' and 'mm_k' with element-wise multiplication.

        On predicting, the model returns the score list between queries and documents.

        Parameters
        ----------
        embed_trainable : bool, optional
            Whether the embeddings should be trained
            if True, the embeddings are trianed
        dropout_rate : float between 0 and 1, optional
            The probability of making a neuron dead
            Used for regularization.
        hidden_sizes : list of int, optional
            The list of hidden sizes for the fully connected layers connected to the matching matrix
            Example :
                hidden_sizes = [10, 20, 30]
            will add 3 fully connected layers of 10, 20 and 30 hidden neurons

        """

        n_layers = len(hidden_sizes)

        query = Input(name='query', shape=(self.text_maxlen,))
        doc = Input(name='doc', shape=(self.text_maxlen,))
        embedding = Embedding(self.embedding_matrix.shape[0], self.embedding_dim,
                              weights=[self.embedding_matrix], trainable=embed_trainable)

        q_embed = embedding(query)
        d_embed = embedding(doc)

        mm = Dot(axes=[2, 2], normalize=True)([q_embed, d_embed])

        # compute term gating
        w_g = Dense(1, activation='softmax')(q_embed)
        g = Reshape((self.text_maxlen,))(w_g)

        mm_k = TopKLayer(topk=self.topk, output_dim=(
            self.text_maxlen, self.embedding_dim))(mm)

        for i in range(n_layers):
            mm_k = Dense(hidden_sizes[i], activation='softplus', kernel_initializer='he_uniform',
                         bias_initializer='zeros')(mm_k)

        mm_k_dropout = Dropout(rate=dropout_rate)(mm_k)

        mm_reshape = Reshape(
            (self.text_maxlen,))(mm_k_dropout)

        mean = Dot(axes=[1, 1])([mm_reshape, g])

        if self.target_mode == 'classification':
            #out_ = Dense(64, activation='relu')(mean)
            out_ = Dense(2, activation='softmax')(mean)
        elif self.target_mode in ['regression', 'ranking']:
            out_ = Reshape((1,))(mean)
        elif self.target_mode == 'inference':
            out_ = Dense(self.num_inferences, activation='softmax')(mean)

        model = Model(inputs=[query, doc], outputs=out_)
        return model

    def evaluate_classification(self, X1, X2, D, batch_size=20):
        """Evaluate a classification model and return the accuracy
        
        Parameters
        ----------
        X1 : list of list of str
        X2 : list of list of str
        D : list of int {1,0}
            whether xi, xj are duplicates

        """
        num_correct = 0
        num_total = 0
        x1_batch, x2_batch, dupl_batch = [], [], []
        test_X, test_Y = [], []
        for x1, x2, d in zip(X1, X2, D):
            x1_batch.append(self._make_indexed(x1))
            x2_batch.append(self._make_indexed(x2))
            dupl_batch.append(to_categorical(d, 2))

            if len(x1_batch) % batch_size == 0:
                test_X.append({'query': np.array(x1_batch), 'doc': np.array(x2_batch)})
                test_Y.append(np.squeeze(np.array(dupl_batch)))

                for tx, ty in zip(test_X, test_Y):
                    this_pred = self.model.predict(tx)
                    for pred_val, true_val in zip(this_pred, ty):
                        if np.argmax(pred_val) == np.argmax(true_val):
                            num_correct += 1
                        num_total += 1

                x1_batch, x2_batch, dupl_batch, x1_len, x2_len = [], [], [], [], []
                test_X, test_Y = [], []

        return num_correct, num_total, num_correct/num_total 

    def evaluate_inference(self, X1, X2, D, batch_size=20):
        """Evaluate an inference model and return the accuracy
        
        Parameters
        ----------
        X1 : list of list of str
        X2 : list of list of str
        D : list of int
            the inference label of xi, xj

        """
        num_correct = 0
        num_total = 0
        x1_batch, x2_batch, dupl_batch = [], [], []
        test_X, test_Y = [], []
        for x1, x2, d in zip(X1, X2, D):
            x1_batch.append(self._make_indexed(x1))
            x2_batch.append(self._make_indexed(x2))
            dupl_batch.append(to_categorical(d, self.num_inferences))

            if len(x1_batch) % batch_size == 0:
                test_X.append({'query': np.array(x1_batch), 'doc': np.array(x2_batch)})
                test_Y.append(np.squeeze(np.array(dupl_batch)))

                for tx, ty in zip(test_X, test_Y):
                    this_pred = self.model.predict(tx)
                    for pred_val, true_val in zip(this_pred, ty):
                        if np.argmax(pred_val) == np.argmax(true_val):
                            num_correct += 1
                        num_total += 1

                x1_batch, x2_batch, dupl_batch, x1_len, x2_len = [], [], [], [], []
                test_X, test_Y = [], []

        return num_correct, num_total, num_correct/num_total
