"""Script to evaluate models on the WikiQA dataset

This script will train the specified model on the WikiQA train set and provide predictions on the WikiQA test set in the TREC format.
The TREC format includes a "qrels" and a "pred" file

It can then be evaluated using
./trec_eval qrels pred

(Get trec_eval from misc_scripts/get_trec.py)

The script will automatically save the qrels and pred file with a distinguishable name.
For example,
pred_mp_wikiqa : pred file of MatchPyramid model on WikiQA test
pred_bidaf_t_wikiqa : pred file of BiDAF_T model on WikiQA test after pretraining on SQUAD-T
pred_bidaf_t_finetuned_wikiqa : pred file of BiDAF_T on WikiQA test after pretraining on SQUAD-T and finetuning on WikiQA train

Since we have a predefined split of WikiQA into test, train and dev (unlike InsuranceQA) the qrels file will always be the
same for all the models.


Example Usage
-------------
$ python eval_wikiqa.py  # evaluates on MatchPyramid, DRMM_TKS, BiDAF_T

$ python eval_wikiqa.py --model_type mp  # evaluates on MatchPyramid

model_type : {mp, dtks, bidaf_t}

mp : MatchPyramid
dtks : DRMM_TKS
bidaf_t : BiDirectional Attention Flow (senTence level)

"""


import sys
sys.path.append('../..')
import sys
import os

from sl_eval.models import MatchPyramid, DRMM_TKS, BiDAF_T
from data_readers import WikiReaderIterable, WikiReaderStatic
import gensim.downloader as api
import argparse

def save_qrels(test_data, fname):
    """Saves the WikiQA data `Truth Data`. This remains the same regardless of which model you use.
    qrels : query relevance

    Format
    ------
    <query_id>\t<0>\t<document_id>\t<relevance>

    Note: parameter <0> is ignored by the model

    Example
    -------
    Q1  0   D1-0    0
    Q1  0   D1-1    0
    Q1  0   D1-2    0
    Q1  0   D1-3    1
    Q1  0   D1-4    0
    Q16 0   D16-0   1
    Q16 0   D16-1   0
    Q16 0   D16-2   0
    Q16 0   D16-3   0
    Q16 0   D16-4   0

    Parameters
    ----------
    fname : str
        File where the qrels should be saved

    """
    queries, doc_group, label_group, query_ids, doc_id_group = test_data
    with open(fname, 'w') as f:
        for q, doc, labels, q_id, d_ids in zip(queries, doc_group, label_group, query_ids, doc_id_group):
            for d, l, d_id in zip(doc, labels, d_ids):
                f.write(q_id + '\t' +  '0' + '\t' +  str(d_id) + '\t' + str(l) + '\n')
    print("qrels done. Saved as %s" % fname)

def save_model_pred(test_data, fname, similarity_fn):
    """Goes through all the queries and docs, gets their Similarity score as per the `similarity_fn`
    and saves it in the TREC format

    Format
    ------
    <query_id>\t<Q0>\t<document_id>\t<rank>\t<model_score>\t<STANDARD>

    Note: parameters <Q0>, <rank> and <STANDARD> are ignored by the model and can be kept as anything
    I have chose 99 as the rank. It has no meaning.

    Example
    -------
    Q1  Q0  D1-0    99  0.64426434  STANDARD
    Q1  Q0  D1-1    99  0.26972288  STANDARD
    Q1  Q0  D1-2    99  0.6259719   STANDARD
    Q1  Q0  D1-3    99  0.8891963   STANDARD
    Q1  Q0  D1-4    99  1.7347554   STANDARD
    Q16 Q0  D16-0   99  1.1078827   STANDARD
    Q16 Q0  D16-1   99  0.22940424  STANDARD
    Q16 Q0  D16-2   99  1.7198141   STANDARD
    Q16 Q0  D16-3   99  1.7576259   STANDARD
    Q16 Q0  D16-4   99  1.548423    STANDARD

    Parameters
    ----------
    fname : str
        File where the qrels should be saved

    similarity_fn : function
        Parameters
            - query : list of str
            - doc : list of str
        Returns
            - similarity_score : float
    """
    queries, doc_group, label_group, query_ids, doc_id_group = test_data
    with open(fname, 'w') as f:
        for q, doc, labels, q_id, d_ids in zip(queries, doc_group, label_group, query_ids, doc_id_group):
            for d, l, d_id in zip(doc, labels, d_ids):
                my_score = str(similarity_fn(q,d))
                f.write(q_id + '\t' + 'Q0' + '\t' + str(d_id) + '\t' + '99' + '\t' + my_score + '\t' + 'STANDARD' + '\n')
    print("Prediction done. Saved as %s" % fname)

def dtks_similarity_fn(q, d):
    """Similarity Function for DRMM TKS

    Parameters
    ----------
    query : list of str
    doc : list of str

    Returns
    -------
    similarity_score : float
    """
    return drmm_tks_model.predict([q], [[d]])[0][0]

def mp_similarity_fn(q, d):
    """Similarity Function for DRMM TKS

    Parameters
    ----------
    query : list of str
    doc : list of str

    Returns
    -------
    similarity_score : float
    """
    return mp_model.predict([q], [[d]])[0][0]


if __name__ == '__main__':
    wikiqa_folder = os.path.join('..', '..', 'data', 'WikiQACorpus')


    squad_t_path = os.path.join('..', '..', 'data', 'SQUAD-T-QA.tsv')

    if not os.path.exists(squad_t_path):
        raise ValueError('The SQUAD-T_QA.tsv file is missing. Please read misc_scripts/squad2QA.py and run it.')


    parser = argparse.ArgumentParser()
    parser.add_argument('--model_type', required=False, help='the model to be evaluated (mp, dtks, bidaf_t)')
    args = parser.parse_args()

    model_type = args.model_type

    do_bidaf_t, do_mp, do_dtks = False, False, False
    if model_type == 'dtks':
        do_bidaf_t = True
    elif model_type == 'mp':
        do_mp = True
    elif model_type == 'bidaf_t':
        do_bidaf_t = True
    else:  # Evaluate all
        do_bidaf_t, do_mp, do_dtks = True, True, True
        


    q_iterable = WikiReaderIterable('query', os.path.join(wikiqa_folder, 'WikiQA-train.tsv'))
    d_iterable = WikiReaderIterable('doc', os.path.join(wikiqa_folder, 'WikiQA-train.tsv'))
    l_iterable = WikiReaderIterable('label', os.path.join(wikiqa_folder, 'WikiQA-train.tsv'))

    q_val_iterable = WikiReaderIterable('query', os.path.join(wikiqa_folder, 'WikiQA-dev.tsv'))
    d_val_iterable = WikiReaderIterable('doc', os.path.join(wikiqa_folder, 'WikiQA-dev.tsv'))
    l_val_iterable = WikiReaderIterable('label', os.path.join(wikiqa_folder, 'WikiQA-dev.tsv'))

    q_test_iterable = WikiReaderIterable('query', os.path.join(wikiqa_folder, 'WikiQA-test.tsv'))
    d_test_iterable = WikiReaderIterable('doc', os.path.join(wikiqa_folder, 'WikiQA-test.tsv'))
    l_test_iterable = WikiReaderIterable('label', os.path.join(wikiqa_folder, 'WikiQA-test.tsv'))

    test_data = WikiReaderStatic(os.path.join(wikiqa_folder, 'WikiQA-test.tsv')).get_data()

    num_samples_wikiqa = 9000
    num_embedding_dims = 300
    qrels_save_path = 'qrels_wikiqa'
    mp_pred_save_path = 'pred_mp_wikiqa'
    dtks_pred_save_path = 'pred_dtks_wikiqa'
    bidaf_t_pred_save_path = 'pred_bidaf_t_wikiqa'
    bidaf_t_finetuned_pred_save_path = 'pred_bidaf_t_finetuned_wikiqa'
    
    print('Saving qrels for WikiQA test data')
    save_qrels(test_data, qrels_save_path)

    kv_model = api.load('glove-wiki-gigaword-' + str(num_embedding_dims))



    if do_bidaf_t:
        q_squad = WikiReaderIterable('query', squad_t_path)
        d_squad = WikiReaderIterable('doc', squad_t_path)
        l_squad = WikiReaderIterable('label', squad_t_path)


        num_squad_samples = 447551

        n_epochs = 2


        batch_size = 100
        text_maxlen = 100
        steps_per_epoch_squad = num_squad_samples // batch_size

        print('Pretraining on SQUAD-T dataset')
        bidaf_t_model = BiDAF_T(q_squad, d_squad, l_squad, kv_model, n_epochs=n_epochs,
                                steps_per_epoch=steps_per_epoch_squad)


        print('Testing on WikiQA-test')
        queries, doc_group, label_group, query_ids, doc_id_group = test_data
        i=0
        with open(bidaf_t_pred_save_path, 'w') as f:
            for q, doc, labels, q_id, d_ids in zip(queries, doc_group, label_group, query_ids, doc_id_group):
                batch_score = bidaf_t_model.batch_predict(q, doc)
                for d, l, d_id, bscore in zip(doc, labels, d_ids, batch_score):
                    my_score = bscore[1]
                    i += 1
                    f.write(q_id + '\t' + 'Q0' + '\t' + str(d_id) + '\t' + '99' + '\t' + str(my_score) + '\t' + 'STANDARD' + '\n')
        print("Prediction done. Saved as %s" % bidaf_t_pred_save_path)


        print('FineTuning on WikiQA-train set')
        finetune_epochs = 1
        finetune_batch_size = 100
        steps_per_epoch = num_samples_wikiqa // finetune_batch_size
        bidaf_t_model.train(queries=q_iterable, docs=d_iterable, labels=l_iterable, batch_size=finetune_batch_size,
                            steps_per_epoch=steps_per_epoch, n_epochs=finetune_epochs)



        print('Testing on WikiQA-test after finetuning')
        queries, doc_group, label_group, query_ids, doc_id_group = test_data
        i=0
        with open(bidaf_t_finetuned_pred_save_path, 'w') as f:
            for q, doc, labels, q_id, d_ids in zip(queries, doc_group, label_group, query_ids, doc_id_group):
                batch_score = bidaf_t_model.batch_predict(q, doc)
                for d, l, d_id, bscore in zip(doc, labels, d_ids, batch_score):
                    my_score = bscore[1]
                    i += 1
                    f.write(q_id + '\t' + 'Q0' + '\t' + str(d_id) + '\t' + '99' + '\t' + str(my_score) + '\t' + 'STANDARD' + '\n')
        print("Prediction done. Saved as %s" % bidaf_t_finetuned_pred_save_path)        

    if do_mp:
        n_epochs = 2 
        batch_size = 10
        text_maxlen = 100
        steps_per_epoch = num_samples_wikiqa // batch_size

        # Train the model
        mp_model = MatchPyramid(
                            queries=q_iterable, docs=d_iterable, labels=l_iterable, word_embedding=kv_model,
                            epochs=n_epochs, steps_per_epoch=steps_per_epoch, batch_size=batch_size, text_maxlen=text_maxlen,
                            unk_handle_method='zero'
                        )

        print('Test set results')
        mp_model.evaluate(q_test_iterable, d_test_iterable, l_test_iterable)

        print('Saving prediction on test data in TREC format')
        save_model_pred(test_data, mp_pred_save_path, mp_similarity_fn)

    if do_dtks:
        batch_size = 10
        steps_per_epoch = num_samples_wikiqa // batch_size
        n_epochs = 6 

        # Train the model
        drmm_tks_model = DRMM_TKS(
                            queries=q_iterable, docs=d_iterable, labels=l_iterable, word_embedding=kv_model, epochs=n_epochs,
                            topk=20, steps_per_epoch=steps_per_epoch, batch_size=batch_size
                        )

        print('Test set results')
        drmm_tks_model.evaluate(q_test_iterable, d_test_iterable, l_test_iterable)

        print('Saving prediction on test data in TREC format')
        save_model_pred(test_data, dtks_pred_save_path, dtks_similarity_fn)

