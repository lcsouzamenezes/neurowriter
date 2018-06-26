#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jul 30 13:49:48 2017

Functions for tokenizing texts.

All tokenizers are defined as classes that must feature a fit and transform
method.

@author: Álvaro Barbero Jiménez
"""

import re
import collections
from itertools import chain

from neurowriter.linkedlist import LinkedList


class CharTokenizer:
    """Tokenizer that splits a text into its basic characters"""

    @staticmethod
    def fit(corpus):
        # No training necessary
        pass

    @staticmethod
    def transform(text):
        return list(text)

    def __eq__(self, other):
        return isinstance(other, CharTokenizer)


class WordTokenizer:
    """Tokenizer that splits text in words
    
    Punctuation and whitespace symbols are kept as individual
    tokens, so the input text can be rebuild by just concatenating
    all tokens.
    
    Words that have a low number of occurrences in the text are broken
    down to individual characters, to reduce the overall number of tokens.
    """
    
    def __init__(self, numsymbols=4096, minfreq=2):
        self.numsymbols = numsymbols
        self.minfreq = minfreq
        self.symbols = None
        # Precompile parsing expression
        self.parser = re.compile('(\W)')
    
    def fit(self, corpus):
        # First add all basic characters to the dictionary
        self.symbols = set(chain(*[doc for doc in corpus]))
        # Split input in words, get unique tokens and counts
        tokens = collections.Counter(
            chain(*[self.parser.split(doc) for doc in corpus])
        )
        # Filter out unfrequent symbols
        freqsymbols = [(symbol, freq) for symbol, freq in tokens.items() 
                        if freq >= self.minfreq]
        # Sort remaining symbols by frequency
        srt = sorted(freqsymbols, key=lambda x: x[1], reverse=True)
        # Remove already included characters
        remain = [symbol for symbol, freq in srt if symbol not in self.symbols]
        # Fill the dictionary with symbols until max allowed
        freespace = self.numsymbols - len(self.symbols)
        self.symbols.update(remain[0:freespace])
    
    def transform(self, text):
        # Break input in words
        tokens = self.parser.split(text)
        # For every word not in the recognized symbols list, break into chars
        # If a character has never been seen before, it is ignored
        result = []
        for token in tokens:
            if token in self.symbols:
                result.append(token)
            else:
                for char in token:
                    if char in self.symbols:
                        result.append(char)
        return result

    def __eq__(self, other):
        if not isinstance(other, WordTokenizer):
            return False
        return self.symbols == other.symbols


class SubwordTokenizer:
    """Abstract tokenizer that splits text in descriptive subword parts

    Subclasses must implement the fit method.
    """
    def __init__(self):
        # Symbols (subwords) recognized by this tokenizer
        self.symbols = None
        # Precompiled expression that accelerates transformation
        self.detector = None

    def bestmatch(self, string):
        """Find the best matching symbol at the beginning of a string"""
        # Precompile matcher if needed
        if self.detector is None:
            if self.symbols is None:
                raise ValueError("Tokenizer has not been fitted")
            else:
                self.compile()
        match = self.detector.match(string)
        if match is not None:
            return match.group()
        else:
            return None

    def compile(self):
        """Compiles the subword tokenizer parsing the subword symbols for more efficiency"""
        # Sort symbols by length, so larger symbols have precedence
        srt = sorted(self.symbols, key=lambda x: len(x), reverse=True)
        # Escape special symbols
        srt = [re.escape(token) for token in srt]
        # Detect any symbol, with precedence for larger ones
        self.detector = re.compile('|'.join(srt))

    def fit(self, corpus):
        raise NotImplementedError

    def transform(self, text):
        transformed = []
        i = 0
        while i < len(text):
            symbol = self.bestmatch(text[i:])
            transformed.append(symbol)
            i += len(symbol)
        return transformed

    def __eq__(self, other):
        if not isinstance(other, SubwordTokenizer):
            return False
        return self.symbols == other.symbols


class BPETokenizer(SubwordTokenizer):
    """Tokenizer that splits text in descriptive subword parts, using the BPE algorithm.

    Subword parts are trained for each corpus, building from single
    characters and using a Byte Pair Encoding (BPE) method.

    References:
        - https://en.wikipedia.org/wiki/Byte_pair_encoding
        - https://github.com/rsennrich/subword-nmt
        - https://arxiv.org/abs/1508.07909
    """

    def __init__(self, numsymbols=4096, minfreq=10, crosswords=False):
        """Creates a Byte Pair Encoding Subword Tokenizer

        Arguments:
            numsymbols: maximum number of symbols to generate
            minfreq: minimum frequency for a string of characters to be made into a symbol
            crosswords: whether to allow generated symbols to cross word boundaries
        """
        self.numsymbols = numsymbols
        self.minfreq = minfreq
        self.crosswords = crosswords
        super().__init__()

    def validpair(self, s1, s2):
        """Checks that a pair a symbols is valid for joining

        Essentially amounts to checking that neither symbol is crossing a word boundary, if such option is
        active.
        """
        # If crosswords option is active, we can join anything
        # If both are already a composite symbol, it's also ok to join
        if self.crosswords or (len(s1) > 1 and len(s2) > 1):
            return True
        # If any of them are characters, check that both are valid word symbols
        else:
            return (len(s1) > 1 or re.match("\w", s1)) and (len(s2) > 1 or re.match("\w", s2))

    def pairfreqs(self, corpus):
        """Computes symbol pair statistics over a corpus

        The input must be a list of docs, each a LinkedList of symbols.
        Statistics over words won't be accounted for if the crosswords options is disabled.
        """
        stats = collections.defaultdict(int)
        for doc in corpus:
            for node in doc.iternodes():
                # Only account for non-word symbols of crosswords option is active
                if node.nxt is not None and self.validpair(node.value, node.nxt.value):
                    stats[node.value, node.nxt.value] += 1
        return stats

    def mergesymbols(self, corpus, symbols, freqs, leftsymbol, rightsymbol):
        """Merges two symbols in the encoding

        Arguments:
            - corpus: current list of docs, each a LinkedList of symbols
            - symbols: current set of symbols
            - freqs: current symbol pairs statistics
            - leftsymbol, rightsymbol: symbols to merge

        Returns:
            - new corpus with merged symbols
            - new list of symbols
            - updated symbol pair statistics
        """
        # Add new symbol to set
        newsymbol = leftsymbol + rightsymbol
        self.symbols.add(newsymbol)

        # Go over each doc in corpus, find occurrences of the given pair and merge
        for doc in corpus:
            for node in doc.iternodes():
                if node.value == leftsymbol and node.nxt is not None and node.nxt.value == rightsymbol:
                    node.mergewithnext()
                    # Update frequencies with previous symbol
                    if node.prev is not None and self.validpair(node.prev.value, newsymbol):
                        prevsymbol = node.prev.value
                        freqs[prevsymbol, newsymbol] += 1
                        freqs[prevsymbol, leftsymbol] -= 1
                    # Update frequencies with next symbol
                    if node.nxt is not None and self.validpair(node.nxt.value, newsymbol):
                        nextsymbol = node.nxt.value
                        freqs[newsymbol, nextsymbol] += 1
                        freqs[rightsymbol, nextsymbol] -= 1

        # Delete statistics of merged symbols
        del freqs[(leftsymbol, rightsymbol)]

        return corpus, freqs, symbols

    def prunesymbols(self, corpus):
        """Removes from the list of symbols those that appear unfrequently in the corpus

        This is useful after performing all the merge operations, where some symbols might
        have dissapeared from the corpus aftar being merged with others.

        The provided corpus must be an iterable of documents, each a Linked List of symbols
        after all merge operations.

        Symbols made of 1 character are never removed.
        """
        # Compute frequencies of the provided corpus
        freqs = collections.defaultdict(int)
        for doc in corpus:
            for symbol in doc:
                freqs[symbol] += 1
        # Go over the symbols in the tokenizer, remove those with low frequency and more than 1 char
        self.symbols = {symbol for symbol in self.symbols if len(symbol) == 1 or freqs[symbol] >= self.minfreq}

    def mergingrun(self, corpus, freqs):
        """Performs symbol merge operations till a max number of symbols is reached, or too infrequent symbols appear"""
        while len(self.symbols) < self.numsymbols:
            # Find most frequent pair
            leftsymbol, rightsymbol = max(freqs, key=freqs.get)
            # If most frequent is too infrequent, stop procedure
            if freqs[(leftsymbol, rightsymbol)] < self.minfreq:
                return corpus, freqs
            # Merge symbols
            corpus, freqs, self.symbols = self.mergesymbols(
                corpus,
                self.symbols,
                freqs,
                leftsymbol,
                rightsymbol
            )
        return corpus, freqs

    def fit(self, corpus):
        # Cast corpus to list of linked-lists of symbols
        corpus = [LinkedList(doc) for doc in corpus]
        # Initialize symbols with chars
        self.symbols = set(chain(*[doc for doc in corpus]))
        # Compute char pairs frequencies
        freqs = self.pairfreqs(corpus)
        # Merge steps until maximum number of symbols reached
        finished = False
        while not finished:
            # Merge symbols as much as possible
            corpus, freqs = self.mergingrun(corpus, freqs)
            # Now prune the set to remove small symbols that might have been embedded in others
            beforeprune = len(self.symbols)
            self.prunesymbols(corpus)
            afterprune = len(self.symbols)
            # If the prune was effective, try another merge run. Else finish the algorithm
            if beforeprune == afterprune:
                finished = True
        # Compile tokenizer for found symbols
        self.compile()


class SubwordsListTokenizer(SubwordTokenizer):
    """Tokenizer that splits the text following a given list of valid subwords"""
    def __init__(self, subwords):
        """Creates a SubwordListTokenizer through a list of valid subwords"""
        self.subwords = set(subwords)
        super().__init__()

    def fit(self, corpus):
        """Fits the tokenizer using a corpus.

        The tokenizer will recognize all characters present in the corpus
        plus the subwords given at initialization.
        """
        self.symbols = set(chain(*[doc for doc in corpus])) | self.subwords
        self.compile()


"""Dictionary of tokenizers indexed by a string"""
TOKENIZERSBYNAME = {
    "char": CharTokenizer,
    "word": WordTokenizer,
    "bpe": BPETokenizer,
    "subwordslist": SubwordsListTokenizer,
}


def tokenizerbyname(tokenizername):
    """Returns a tokenizer class by name"""
    if tokenizername not in TOKENIZERSBYNAME:
        raise ValueError("Unknown tokenizer %s" % tokenizername)
    return TOKENIZERSBYNAME[tokenizername]
