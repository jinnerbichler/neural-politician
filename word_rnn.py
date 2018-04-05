import codecs
import logging
import os
import sys
from functools import partial
from pathlib import Path
from typing import List

from keras import Sequential, Model
from keras.callbacks import TensorBoard, LambdaCallback, ModelCheckpoint
from keras.layers import Embedding, LSTM, Dense, Dropout
from keras.optimizers import Adam
from tensorflow.python.client import device_lib
import tensorflow as tf
import numpy as np

np.random.seed(1)
tf.set_random_seed(1)

from speech_data import SpeechSequence, Sentence
import speech_data

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger('word_rnn')
logger.setLevel(logging.DEBUG)

TENSORBOARD_LOGS_DIR = os.getenv('TENSORBOARD_LOGS_DIR', './graph/')
MODELS_DIR = os.getenv('MODELS_DIR', './models/')
SEQUENCE_LENGTH = 15
BATCH_SIZE = 128
LSTM_SIZE = 1024
VOCAB_OUTPUT = 5000
EMBEDDING_SIZE = 300  # fixed in pre-trained embeddings


def main():
    sentences = speech_data.extract_sentences(try_cached=True)  # type: List(Sentence)

    if False:
        global BATCH_SIZE
        global LSTM_SIZE
        global VOCAB_OUTPUT
        sentences = sentences[:20]
        BATCH_SIZE = 8
        LSTM_SIZE = 100
        VOCAB_OUTPUT = 150

    # create dataset based on all sentences
    word_vectors = speech_data.extract_word_vectors(sentences=sentences, try_cached=True)
    dataset = SpeechSequence(sentences=sentences, output_size=VOCAB_OUTPUT,
                             batch_size=BATCH_SIZE, word_vectors=word_vectors,
                             sequence_len=SEQUENCE_LENGTH)
    dataset.adapt(sentences=sentences)
    word_vectors = dataset.word_vectors  # added vector of for OOV tokens
    embeddings_path = create_tensorboard_embeddings(dataset=dataset)

    # preparate data
    logger.info('Prepared data with length %d', len(dataset))

    # create generic model
    generic_model_file = Path(MODELS_DIR).joinpath('word_generic.hdf5').absolute()
    model = create_rnn(name='generic', word_vectors=word_vectors,
                       output_size=dataset.output_vocab_size, lstm_size=LSTM_SIZE,
                       sequence_len=SEQUENCE_LENGTH, weights_file=generic_model_file)
    model.summary()

    # train generic model
    logger.info('Training generic model with all sentences')
    train(model=model, dataset=dataset, checkpoint_file=str(generic_model_file),
          embeddings_path=embeddings_path, epochs=400)

    # train specific model for each politician
    for politician in speech_data.POLITICIANS:
        logger.info('Training model for %s', politician)

        # create dataset with sentences from specific politician
        filtered_sentences = [s for s in sentences if s.politician == politician]
        dataset.adapt(filtered_sentences)

        # create specific model
        specific_model_file = Path(MODELS_DIR).joinpath('word_{}.hdf5'.format(politician))
        specific_model_file = specific_model_file.absolute()
        model = create_rnn(name=politician, word_vectors=word_vectors,
                           output_size=dataset.output_vocab_size, lstm_size=LSTM_SIZE,
                           sequence_len=SEQUENCE_LENGTH, weights_file=generic_model_file)
        model.summary()

        # train specific model
        train(model=model, dataset=dataset, checkpoint_file=str(specific_model_file),
              embeddings_path=embeddings_path, epochs=15)


# noinspection PyBroadException
def create_rnn(name, word_vectors, output_size, sequence_len, lstm_size, weights_file):
    vocab_size = len(word_vectors)

    # prepare pre-trained word embeddings
    word_ids = {wv.id: wv for wv in word_vectors.values()}
    embedding_matrix = np.zeros((vocab_size, EMBEDDING_SIZE), dtype=np.float64)
    for i in range(vocab_size):
        embedding_matrix[i] = word_ids[i].vector

    # define model
    model = Sequential(name=name)
    model.add(Embedding(vocab_size, EMBEDDING_SIZE, weights=[embedding_matrix],
                        input_length=sequence_len, trainable=False))
    model.add(LSTM(lstm_size, return_sequences=True))
    # model.add(Dropout(rate=0.1))
    model.add(LSTM(lstm_size))
    # model.add(Dropout(rate=0.1))
    model.add(Dense(output_size, activation='softmax'))

    # try load existing weights
    try:
        if weights_file and Path(weights_file).exists():
            logger.info('Reading weights from %s', weights_file)
            model.load_weights(filepath=weights_file)
            logger.info('Successfully loaded weights from %s', weights_file)
        else:
            logger.info('No stored weights found.')
    except:
        logger.exception('Cannot not read stored weights!')

    # compile network
    optimizer = Adam(lr=0.0001)
    model.compile(loss='categorical_crossentropy', optimizer=optimizer,
                  metrics=['accuracy'])

    return model


def train(model, dataset, checkpoint_file, epochs, embeddings_path):
    # define the checkpoint
    logger.info('Storing weights in %s', checkpoint_file)
    checkpoint_cb = ModelCheckpoint(checkpoint_file, monitor='loss', verbose=1,
                                    save_best_only=True, mode='min')

    # Tensorboard callack
    tensorboard_cb = TensorBoard(log_dir=TENSORBOARD_LOGS_DIR, write_graph=True,
                                 embeddings_metadata=str(embeddings_path),
                                 embeddings_freq=1)
    tensorboard_cb.set_model(model)

    # prediction/validation callback
    predict_cb = partial(epoch_end_prediction, model=model, dataset=dataset)
    predict_cb = LambdaCallback(on_epoch_end=predict_cb)

    # learning rate decay
    # learning_rate_decay_db = LearningRateReducer(reduce_rate=0.1)

    # fit the model
    callbacks = [checkpoint_cb, tensorboard_cb, predict_cb]

    # adapt weights for proper handling of under-represented classes
    word_counts = dataset.output_word_counts
    counts_no_unk = {w: c for w, c in word_counts.items() if w != dataset.oov_token}
    max_count = np.max(list(counts_no_unk.values()))
    min_count = np.min(list(counts_no_unk.values()))

    # normalize weights between 0.66 and 2.0
    def class_weight(word_id):
        word_count = dataset.output_word_counts[dataset.output_word_ids[word_id]]
        return 1.0 / ((word_count - min_count) / (max_count - min_count) + 0.5)

    class_weights = {w: class_weight(w) for w in dataset.output_word_ids}
    class_weights = {w: 1.0 for w in dataset.output_vocab.values()}  # ToDo: remove
    class_weights[dataset.output_unk_id] = 1e-7  # decrease loss for UNK

    model.fit_generator(generator=dataset, epochs=epochs, verbose=1, callbacks=callbacks,
                        shuffle=True, class_weight=class_weights)


def sample(preds, temperature=1.0):
    # helper function to sample an index from a probability array
    preds = np.asarray(preds).astype('float64')
    preds = np.log(preds) / temperature
    exp_preds = np.exp(preds)
    preds = exp_preds / np.sum(exp_preds)
    probas = np.random.multinomial(1, preds, 1)
    return np.argmax(probas)


# noinspection PyUnusedLocal
def epoch_end_prediction(epoch, logs, model, dataset):
    # type: (int, dict, Model, SpeechSequence) -> None
    # Function invoked at end of each epoch. Prints generated text.

    # start_index = np.random.randint(0, len(text) - SEQUENCE_LENGTH - 1)  # ToDo: enable
    start_index = 0
    start_input = dataset.input_encoded[start_index: start_index + SEQUENCE_LENGTH]
    start_output = dataset.output_encoded[start_index: start_index + SEQUENCE_LENGTH]
    decoded_input = dataset.decode_input_string(start_input)
    logger.info('---- Generating text after Epoch: %d Seed: %s' % (epoch, decoded_input))
    for diversity in [0.2]:
        # for diversity in [0.2, 0.5, 1.0, 1.2]:

        current_input = start_input.copy()
        generated_output = start_output.copy()

        for i in range(400):
            x_pred = np.array([current_input])
            preds = model.predict(x_pred, verbose=0)[0]
            preds = preds[:-1]  # remove last entry, which represents unkown words
            next_output_word_id = sample(preds, diversity)

            generated_output.append(next_output_word_id)

            next_input_word_id = dataset.out_to_in(word_id=next_output_word_id)
            current_input = current_input[1:] + [next_input_word_id]

        decoded_output = dataset.decode_output_string(generated_output)
        logger.info('---- Generated text (diversity: %s): %s', diversity, decoded_output)
        sys.stdout.flush()


def create_tensorboard_embeddings(dataset):
    logger.debug('Creating embeddings file for Tensorboard...')

    # storing metadata for TensorBoard
    embeddings_path = Path(TENSORBOARD_LOGS_DIR).joinpath('embeddings_meta.txt')
    embeddings_path.parent.mkdir(exist_ok=True)
    embeddings_path = embeddings_path.absolute()
    with codecs.open(str(embeddings_path), 'w', "utf-8") as embeddings_file:
        lines = ['Word\tIndex\tCount']
        word_ids = {wv.id: wv for wv in dataset.word_vectors.values()}
        for word_id in range(len(word_ids)):
            word = word_ids[word_id].word
            line = '{}\t{}\t{}'.format(word, word_id,
                                       dataset.input_word_counts.get(word, 0))
            lines.append(line)
        embeddings_file.write('\n'.join(lines))

    return embeddings_path.absolute()


if __name__ == '__main__':
    local_device_protos = device_lib.list_local_devices()
    logger.info('Detected devices: {}'.format([d.name for d in local_device_protos]))
    main()
