"""
@author : Hyunwoong
@when : 2019-10-29
@homepage : https://github.com/gusdnd852
"""
from torchtext.legacy.data import Field, BucketIterator
from torchtext.legacy.datasets.translation import Multi30k






class DataLoader:
    source: Field = None
    target: Field = None

    def __init__(self, ext, tokenize_en, tokenize_de, init_token, eos_token):
        self.ext = ext
        self.tokenize_en = tokenize_en
        self.tokenize_de = tokenize_de
        self.init_token = init_token
        self.eos_token = eos_token
        print('dataset initializing start')

    def make_dataset(self):
        if self.ext == ('.de', '.en'):
            self.source = Field(tokenize=self.tokenize_de, init_token=self.init_token, eos_token=self.eos_token,
                                lower=True, batch_first=True)
            self.target = Field(tokenize=self.tokenize_en, init_token=self.init_token, eos_token=self.eos_token,
                                lower=True, batch_first=True)

        elif self.ext == ('.en', '.de'):
            self.source = Field(tokenize=self.tokenize_en, init_token=self.init_token, eos_token=self.eos_token,
                                lower=True, batch_first=True)
            self.target = Field(tokenize=self.tokenize_de, init_token=self.init_token, eos_token=self.eos_token,
                                lower=True, batch_first=True)

        train_data, valid_data, test_data = Multi30k.splits(exts=self.ext, fields=(self.source, self.target))
        return train_data, valid_data, test_data

    def build_vocab(self, train_data, min_freq):
        self.source.build_vocab(train_data, min_freq=min_freq)
        self.target.build_vocab(train_data, min_freq=min_freq)

    def make_iter(self, train, validate, test, batch_size, device):
        train_iterator, valid_iterator, test_iterator = BucketIterator.splits((train, validate, test),
                                                                              batch_size=batch_size,
                                                                              device=device)
        print('dataset initializing done')
        return train_iterator, valid_iterator, test_iterator



if __name__ == '__main__':
    import torch
    from tokenizer import Tokenizer

    # GPU device setting
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # model parameter setting
    batch_size = 128
    max_len = 256
    d_model = 512
    n_layers = 6
    n_heads = 8
    ffn_hidden = 2048
    drop_prob = 0.1

    # optimizer parameter setting
    init_lr = 1e-5
    factor = 0.9
    adam_eps = 5e-9
    patience = 10
    warmup = 100
    epoch = 1000
    clip = 1.0
    weight_decay = 5e-4
    inf = float('inf')

    tokenizer = Tokenizer()
    loader = DataLoader(ext=('.en', '.de'),
                        tokenize_en=tokenizer.tokenize_en,
                        tokenize_de=tokenizer.tokenize_de,
                        init_token='<sos>',
                        eos_token='<eos>')

    train, valid, test = loader.make_dataset()
    loader.build_vocab(train_data=train, min_freq=2)
    train_iter, valid_iter, test_iter = loader.make_iter(train, valid, test,
                                                         batch_size=batch_size,
                                                         device=device)

    src_pad_idx = loader.source.vocab.stoi['<pad>']
    trg_pad_idx = loader.target.vocab.stoi['<pad>']
    trg_sos_idx = loader.target.vocab.stoi['<sos>']

    enc_voc_size = len(loader.source.vocab)
    dec_voc_size = len(loader.target.vocab)

