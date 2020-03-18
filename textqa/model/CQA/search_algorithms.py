# -*- coding: utf-8 -*-

import json
from gensim.summarization.bm25 import BM25
from gensim.models import TfidfModel, KeyedVectors
from gensim.corpora import Dictionary
from gensim.similarities import SparseMatrixSimilarity
from gensim.matutils import jaccard
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import jieba
import pickle
from nltk.lm.preprocessing import *
from nltk.lm.models import KneserNeyInterpolated
from textqa.model.CQA.file_pool import FilePool
from textqa.model.CQA.utils import Utils
from textqa.model.CQA import args

self_trained_word2vec = 'train_embed/word2vec.kv'


class Baselines:
    def __init__(self, use_aver_embed=False, use_pretrained_word2vec=True):
        self.word2vec_pickle = 'data/word2vec.pickle'
        self.base_ques_file = 'data/base_questions.json'
        self.small_ans_file = 'data/small_answers.txt'

        with open(FilePool.stopword_txt, 'r') as f_stopword:
            doc = f_stopword.readlines()
        self.stopwords = [line.rstrip('\n') for line in doc]

        if args.long_ans:
            # 使用长答案
            ans_json = FilePool.long_answers_json
            ans_txt = FilePool.long_answers_txt
        else:
            # 使用短答案
            ans_json = FilePool.cleaned_answers_json
            ans_txt = FilePool.cleaned_answers_txt

        with open(ans_json, 'r') as f_json:
            self.cut_answers = json.load(f_json)
            self.cut_answers = [[ele for ele in answer if ele not in self.stopwords] for answer in self.cut_answers]
        with open(ans_txt, 'r') as f_ans_txt:
            uncut_answers = f_ans_txt.readlines()
            self.uncut_answers = [line.rstrip('\n') for line in uncut_answers]
        with open(self.base_ques_file, 'r') as f_base_ques:
            self.base_questions = json.load(f_base_ques)
        with open(self.small_ans_file, 'r') as f_small_ans:
            small_ans_txt = f_small_ans.readlines()
            self.uncut_small_answers = [line.rstrip('\n') for line in small_ans_txt]

        # 把small answer分词
        self.cut_small_answers = []
        for ans in self.uncut_small_answers:
            line = [w for w in jieba.cut(ans)]
            self.cut_small_answers.append(line)

        # 把被匹配的问题分词，制作一个纯list
        self.base_ques_list = []
        for base_ques in self.base_questions:
            line = [w for w in jieba.cut(base_ques['question'])]
            self.base_ques_list.append(line)

        self.bm25_model = BM25(self.cut_answers)

        if use_aver_embed:
            if use_pretrained_word2vec:
                # 用预训练好的word2vec
                with open(self.word2vec_pickle, 'rb') as f_pickle:
                    self.word2vec = pickle.load(f_pickle)
            else:
                # 用机场文档训练出的word2vec
                self.word2vec = KeyedVectors.load(self_trained_word2vec, mmap='r')

        self.qq_count = 0
        self.qa_count = 0

    # bm25算法搜索
    def bm25(self, query, corpus):
        bm25 = BM25(corpus)
        bm25_weights = bm25.get_scores(query)

        sorted_scores = sorted(bm25_weights, reverse=True)  # 将得分从大到小排序
        sorted_scores = [s / (len(query) + 1) for s in sorted_scores]  # 将得分除以句长
        max_pos = np.argsort(bm25_weights)[::-1]  # 从大到小排序，返回index(而不是真正的value)
        # max_pos = Utils.trim_result(sorted_scores, max_pos, threshold=10)
        answers = self.__max_pos2answers(max_pos, self.uncut_answers)  # 根据max_pos从答案库里把真正的答案抽出来
        return sorted_scores, max_pos, answers

    # 问题-问题匹配
    def qq_match(self, query):
        # 输入tf-idf，得到从大到小排列的index list
        sorted_scores, max_pos, _ = self.tfidf_sim(query, self.base_ques_list)
        answers, questions = self.__max_pos2answers_questions(max_pos)
        return sorted_scores, max_pos, answers, questions

    # QQ匹配和QA匹配混合
    def qq_qa_mix(self, query, threshold=0.7):
        sorted_scores, max_pos, answers, questions = self.qq_match(query)  # 先用QQ匹配试试
        if sorted_scores[0] < threshold:
            # QQ匹配的得分小于阈值，放弃掉QQ匹配，改用QA匹配
            self.qa_count += 1
            # QA匹配暂时选用bm25算法
            # 最后一个返回值没有意义，因为它是按照答案库挑出的答案，但是这里的max_pos根本就不是答案库的index序列
            # 而是base_question的index序列，于是需要下一行的self.__max_pos2answers_questions()方法根据
            # base_question给出实际的答案
            sorted_scores, max_pos, _ = self.bm25(query, self.cut_small_answers)
            answers = self.__max_pos2answers(max_pos, self.uncut_small_answers)
            qa_threshold = 3.0
            filter_answers = []
            for answer, score in zip(answers[:3], sorted_scores[:3]):
                if score > qa_threshold:
                    filter_answers.append(answer)
            return sorted_scores, max_pos, filter_answers, []  # questions的位置返回一个空list
        else:
            # QQ匹配效果不错，直接返回结果
            self.qq_count += 1
            filter_answers = []
            for answer, score in zip(answers[:3], sorted_scores[:3]):
                if score >= threshold:
                    filter_answers.append(answer)
            return sorted_scores, max_pos, filter_answers, questions

    # tf-idf相似度算法搜索
    def tfidf_sim(self, query, corpus):
        # 构造bag of words
        dict = Dictionary(corpus)  # fit dictionary
        n_features = len(dict.token2id)
        bow = [dict.doc2bow(line) for line in corpus]  # convert corpus to BoW format
        query_bow = [dict.doc2bow(query)]
        # 构造tf-idf模型
        model = TfidfModel(bow)  # fit model
        text_tfidf = model[bow]  # apply model
        query_tfidf = model[query_bow]
        sim_index = SparseMatrixSimilarity(text_tfidf, n_features)
        similarities = sim_index.get_similarities(query_tfidf)[0]

        sorted_scores = sorted(similarities, reverse=True)  # 将得分从大到小排序
        max_pos = np.argsort(similarities)[::-1]  # 从大到小排序，返回index(而不是真正的value)
        answers = self.__max_pos2answers(max_pos, self.cut_small_answers)  # 根据max_pos从答案库里把真正的答案抽出来
        return sorted_scores, max_pos, answers

    # 词向量平均
    def aver_embed(self, query):
        doc_score = []

        words = [w for w in query if w in self.word2vec.vocab]  # remove out-of-vocabulary words
        query_token = np.mean(self.word2vec[words], axis=0)  # average embedding of words in the query
        for ans in self.cut_answers:
            words = [w for w in ans if w in self.word2vec.vocab]
            ans_token = np.mean(self.word2vec[words], axis=0)
            cos_sim = cosine_similarity(query_token.reshape(1, -1), ans_token.reshape(1, -1))
            doc_score.append(np.asscalar(cos_sim))

        sorted_scores = sorted(doc_score, reverse=True)  # 将得分从大到小排序
        max_pos = np.argsort(doc_score)[::-1]  # 从大到小排序，返回index(而不是真正的value)
        answers = self.__max_pos2answers(max_pos, self.cut_small_answers)  # 根据max_pos从答案库里把真正的答案抽出来
        return sorted_scores, max_pos, answers

    # Language Model
    def language_model(self, query):
        doc_score = []
        for text in self.cut_answers:
            train, vocab = padded_everygram_pipeline(order=1, text=text)

            lm = KneserNeyInterpolated(1)  # 实例化模型
            lm.fit(train, vocab)  # 喂训练数据

            score = 1
            for word in query:
                score *= lm.score(word)

            doc_score.append(score)

        sorted_scores = sorted(doc_score, reverse=True)  # 将得分从大到小排序
        max_pos = np.argsort(doc_score)[::-1]  # 从大到小排序，返回index(而不是真正的value)
        answers = self.__max_pos2answers(max_pos, self.cut_small_answers)  # 根据max_pos从答案库里把真正的答案抽出来
        return sorted_scores, max_pos, answers

    # 根据max_pos从答案库里把真正的答案抽出来
    def __max_pos2answers(self, max_pos, answer_base):
        max_pos = max_pos.tolist()  # ndarray -> list
        answers = []
        for r in max_pos:
            if r != -1:
                answers.append(answer_base[r])
            else:
                answers.append('-')  # 丢弃该回答
        return answers

    def __max_pos2answers_questions(self, max_pos):
        max_pos = max_pos.tolist()  # ndarray -> list
        answers = []
        questions = []
        for r in max_pos:
            if r != -1:
                answers.append(self.base_questions[r]['sentence'])
                questions.append(self.base_questions[r]['question'])
            else:
                answers.append('-')  # 丢弃该回答
                questions.append('-')  # 丢弃该问题
        return answers, questions
