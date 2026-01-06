#-- pvnet/model_nn.py, lic, 20241212
#-- LSTM/GRU/AGRU/GAT/TCN/HIST/TRANSFORMER modified from MSRA/qlib
import os
os.environ['OMP_NUM_THREADS'] = '1'
import numpy as np
import pandas as pd
import math
import copy
import random
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.nn.utils import weight_norm

#-- LSTM
class LSTMModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1):
        super().__init__()
        self.rnn = nn.LSTM(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout,
        )
        # self.fc_out = nn.Linear(hidden_size, 1)
        self.fc1 = torch.nn.Linear(hidden_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.d_feat = d_feat

    def forward(self, x):
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        out, hidden = self.rnn(x)
        # res_out = self.fc_out(out[:, -1, :]).squeeze()
        res = self.fc1(out[:, -1, :])
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- GRU
class GRUModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1):
        super().__init__()
        self.rnn = nn.GRU(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout,
        )
        # self.fc_out = nn.Linear(hidden_size, 1)
        self.fc1 = torch.nn.Linear(hidden_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.d_feat = d_feat
    
    def forward(self, x):
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        out, hidden = self.rnn(x)
        # res_out = self.fc_out(out[:, -1, :]).squeeze()
        res = self.fc1(out[:, -1, :])
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- BiGRU(Bidirectional GRU)
class BiGRUModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1):
        super().__init__()
        self.rnn = nn.GRU(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout,
        )
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        # self.fc_out = nn.Linear(hidden_size*2, 1)
        self.fc1 = torch.nn.Linear(hidden_size*2, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.d_feat = d_feat
    
    def forward(self, x):
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        out, hidden = self.rnn(x)
        # res = self.fc_out(torch.cat([hidden[-1], hidden[-2]], dim=1))
        # res = self.fc1(torch.cat([hidden[-1], hidden[-2]], dim=1))
        hidden_view = hidden.view(self.num_layers, 2, hidden.shape[1], self.hidden_size)
        last_hidden = hidden_view[-1]
        res = self.fc1(torch.cat([last_hidden[0], last_hidden[1]], dim=1))
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- AGRU(Attention-GRU)
class AGRUModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1, rnn_type="GRU"):
        super().__init__()
        self.hidden_size = hidden_size
        self.input_size = d_feat
        self.dropout = dropout
        self.rnn_type = rnn_type
        self.rnn_layer = num_layers
        self._build_model()

    def _build_model(self):
        try:
            klass = getattr(nn, self.rnn_type.upper())
        except Exception as e:
            raise ValueError("unknown rnn_type `%s`" % self.rnn_type) from e
        self.net = nn.Sequential()
        self.net.add_module("fc_in", nn.Linear(in_features=self.input_size, out_features=self.hidden_size))
        self.net.add_module("act", nn.Tanh())
        self.rnn = klass(
            input_size=self.hidden_size,
            hidden_size=self.hidden_size,
            num_layers=self.rnn_layer,
            batch_first=True,
            dropout=self.dropout,
        )
        # self.fc_out = nn.Linear(in_features=self.hidden_size*2, out_features=1)
        self.fc1 = nn.Linear(in_features=self.hidden_size*2, out_features=self.hidden_size)
        self.bn1 = nn.BatchNorm1d(self.hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.att_net = nn.Sequential()
        self.att_net.add_module(
            "att_fc_in",
            nn.Linear(in_features=self.hidden_size, out_features=int(self.hidden_size/2)),
        )
        self.att_net.add_module("att_dropout", torch.nn.Dropout(self.dropout))
        self.att_net.add_module("att_act", nn.Tanh())
        self.att_net.add_module(
            "att_fc_out",
            nn.Linear(in_features=int(self.hidden_size/2), out_features=1, bias=False),
        )
        self.att_net.add_module("att_softmax", nn.Softmax(dim=1))

    def forward(self, inputs):
        # inputs: [batch_size, input_size*input_day]
        # inputs = inputs.reshape(len(inputs), self.input_size, -1)
        # inputs = inputs.permute(0, 2, 1)  # [batch, input_size, seq_len] -> [batch, seq_len, input_size]
        rnn_out, _ = self.rnn(self.net(inputs))  # [batch, seq_len, num_directions * hidden_size]
        attention_score = self.att_net(rnn_out)  # [batch, seq_len, 1]
        out_att = torch.mul(rnn_out, attention_score)
        out_att = torch.sum(out_att, dim=1)
        # out = self.fc_out(
        #     torch.cat((rnn_out[:, -1, :], out_att), dim=1)
        # )  # [batch, seq_len, num_directions * hidden_size] -> [batch, 1]
        # res = out[..., 0]
        # return res 
        res = self.fc1(torch.cat((rnn_out[:, -1, :], out_att), dim=1))
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- AGRUS(Attention-GRU Simplified)
class SelfAttention(nn.Module):
    def __init__(self, hidden_size):
        super(SelfAttention, self).__init__()
        self.attn = nn.Linear(hidden_size*2, hidden_size)
        self.v = nn.Linear(hidden_size, 1, bias=False)

    def forward(self, hidden, encoder_outputs):
        max_len = encoder_outputs.size(1)
        repeated_hidden = hidden.unsqueeze(1).repeat(1, max_len, 1)
        energy = torch.tanh(self.attn(torch.cat((repeated_hidden, encoder_outputs), dim=2)))
        attention_scores = self.v(energy).squeeze(2)
        attention_weights = nn.functional.softmax(attention_scores, dim=1)
        context_vector = (encoder_outputs*attention_weights.unsqueeze(2)).sum(dim=1)
        return context_vector, attention_weights

class AGRUSModel(nn.Module):
    def __init__(self, d_feat, hidden_size=64, num_layers=2, dropout=0.1):
        super(AGRUSModel, self).__init__()
        self.num_layers = num_layers
        self.rnn = nn.GRU(d_feat, hidden_size=hidden_size, num_layers=num_layers, batch_first=True, dropout=dropout)
        self.attn = SelfAttention(hidden_size)
        self.dropout = nn.Dropout(dropout)
        self.fc1 = nn.Linear(hidden_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)

    def forward(self, x):
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        out, hidden = self.rnn(x)
        attention_scores, attention_weights = self.attn(out[:, -1, :], out)
        attention_scores = self.dropout(attention_scores)
        res = self.fc1(attention_scores)
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- AGRUM(Attention-GRU siMplified)
class ScaledDotProductAttention(nn.Module):
    def __init__(self, temperature):
        super().__init__()
        self.temperature = temperature
        self.relu = nn.ReLU()

    def forward(self, q, v):
        attn_w = torch.matmul(q/self.temperature, q.transpose(1, 0)) # k.transpose(1, 0)
        attn_w = self.relu(attn_w)
        attn_w = attn_w/attn_w.sum(dim=1)
        # attn_w = nn.functional.softmax(attn_w, dim=1)
        output = torch.matmul(attn_w, v)
        return output, attn_w

class MultiHeadAttention(nn.Module):
    def __init__(self, n_head, d_model, d_q, d_v):
        super().__init__()
        self.n_head = n_head
        self.d_q = d_q
        self.d_v = d_v
        self.w_qs = nn.Linear(d_model, n_head*d_q, bias=False)
        self.w_vs = nn.Linear(d_model, n_head*d_v, bias=False)
        self.attention = ScaledDotProductAttention(temperature=1) # sqrt(d_k)

    def forward(self, q, v):
        q = self.w_qs(q) # project & reshape
        v = self.w_vs(v)
        q, attn_w = self.attention(q, v)
        return q, attn_w

class AGRUMModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1):
        super(AGRUMModel, self).__init__()
        self.rnn = nn.GRU(d_feat, hidden_size=hidden_size, num_layers=num_layers, batch_first=True, dropout=dropout)
        self.attn = MultiHeadAttention(n_head=1, d_model=hidden_size, d_q=hidden_size, d_v=hidden_size)
        self.fc1 = torch.nn.Linear(hidden_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)

    def forward(self, input, theta=[0.5,1]):
        out, _ = self.rnn(input)
        hidden = out[:, -1, :]
        attention_scores, attention_weights = self.attn(hidden, hidden)
        res = attention_scores*theta[0]+hidden*theta[1]
        res = self.fc1(res)
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- ResGRU(ResNet1D+GRU)
class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3):
        super(ResidualBlock, self).__init__()
        self.conv1 = nn.Conv1d(in_channels, int(out_channels/2), kernel_size=kernel_size)
        # self.bn1 = nn.BatchNorm1d(int(out_channels/2))
        # self.relu = nn.ReLU(inplace=True)
        self.relu = nn.ELU()
        self.conv2 = nn.Conv1d(int(out_channels/2), out_channels, kernel_size=kernel_size)
        # self.bn2 = nn.BatchNorm1d(out_channels)
        self.downsample = nn.Conv1d(in_channels, out_channels, kernel_size=5) if in_channels != out_channels else None
    
    def forward(self, x):
        out = self.conv1(x)
        # out = self.bn1(out)
        out = self.relu(out)
        out = self.conv2(out)
        # out = self.bn2(out)
        residual = x if self.downsample is None else self.downsample(x)
        out += residual
        # out = self.relu(out)
        return out

class ResGRUModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1, d_resnetout=16): # num_blocks=3
        super(ResGRUModel, self).__init__()
        self.d_feat = d_feat
        self.resnet = ResidualBlock(d_feat, d_resnetout)
        self.rnn = nn.GRU(d_resnetout, hidden_size=hidden_size, num_layers=num_layers, batch_first=True, dropout=dropout)
        self.fc1 = torch.nn.Linear(hidden_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)

    def forward(self, x):
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        x = x.permute(0, 2, 1) # [N, F, T]
        x = self.resnet(x)     # [N, F->RESNETOUT, T]
        x = x.permute(0, 2, 1) # [N, T, RESNETOUT]
        out, hidden = self.rnn(x)
        res = self.fc1(out[:, -1, :])
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- GAT(Graph Attention Networks)
class GATModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1, base_model="GRU"):
        super().__init__()
        if base_model == "GRU":
            self.rnn = nn.GRU(
                input_size=d_feat,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
            )
        elif base_model == "LSTM":
            self.rnn = nn.LSTM(
                input_size=d_feat,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
            )
        else:
            raise ValueError("unknown base model name `%s`" % base_model)
        self.hidden_size = hidden_size
        self.d_feat = d_feat
        self.transformation = nn.Linear(self.hidden_size, self.hidden_size)
        self.a = nn.Parameter(torch.randn(self.hidden_size * 2, 1))
        self.a.requires_grad = True
        self.fc = nn.Linear(self.hidden_size, self.hidden_size)
        # self.fc_out = nn.Linear(hidden_size, 1)
        self.fc1 = nn.Linear(in_features=self.hidden_size, out_features=self.hidden_size)
        self.bn1 = nn.BatchNorm1d(self.hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.leaky_relu = nn.LeakyReLU()
        self.softmax = nn.Softmax(dim=1)

    def cal_attention(self, x, y): # adj
        x = self.transformation(x)
        y = self.transformation(y)
        sample_num = x.shape[0]
        dim = x.shape[1]
        e_x = x.expand(sample_num, sample_num, dim)
        e_y = torch.transpose(e_x, 0, 1)
        attention_in = torch.cat((e_x, e_y), 2).view(-1, dim * 2)
        self.a_t = torch.t(self.a)
        attention_out = self.a_t.mm(torch.t(attention_in)).view(sample_num, sample_num)
        attention_out = self.leaky_relu(attention_out)
        # zero_vec = (-9e15)*torch.ones_like(attention_out)
        # attention_out = torch.where(adj > 0, attention_out, zero_vec)
        att_weight = self.softmax(attention_out)
        return att_weight

    def forward(self, x): # adj
        # x: [N, F*T]
        # x = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        # x = x.permute(0, 2, 1)  # [N, T, F]
        out, _ = self.rnn(x)
        hidden = out[:, -1, :]
        att_weight = self.cal_attention(hidden, hidden)
        hidden = att_weight.mm(hidden) + hidden
        hidden = self.fc(hidden)
        hidden = self.leaky_relu(hidden)
        # res = self.fc_out(hidden).squeeze()
        res = self.fc1(hidden)
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- TCN(Temporal Convolutional Network)
class Chomp1d(nn.Module):
    def __init__(self, chomp_size):
        super(Chomp1d, self).__init__()
        self.chomp_size = chomp_size

    def forward(self, x):
        return x[:, :, : -self.chomp_size].contiguous()

class TemporalBlock(nn.Module):
    def __init__(self, n_inputs, n_outputs, kernel_size, stride, dilation, padding, dropout=0.2):
        super(TemporalBlock, self).__init__()
        self.conv1 = weight_norm(
            nn.Conv1d(n_inputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation)
        )
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = weight_norm(
            nn.Conv1d(n_outputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation)
        )
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.net = nn.Sequential(
            self.conv1, self.chomp1, self.relu1, self.dropout1, self.conv2, self.chomp2, self.relu2, self.dropout2
        )
        self.downsample = nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        self.relu = nn.ReLU()
        self.init_weights()

    def init_weights(self):
        self.conv1.weight.data.normal_(0, 0.01)
        self.conv2.weight.data.normal_(0, 0.01)
        if self.downsample is not None:
            self.downsample.weight.data.normal_(0, 0.01)

    def forward(self, x):
        out = self.net(x)
        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)

class TemporalConvNet(nn.Module):
    def __init__(self, num_inputs, num_channels, kernel_size=2, dropout=0.2):
        super(TemporalConvNet, self).__init__()
        layers = []
        num_levels = len(num_channels)
        for i in range(num_levels):
            dilation_size = 2**i
            in_channels = num_inputs if i == 0 else num_channels[i - 1]
            out_channels = num_channels[i]
            layers += [
                TemporalBlock(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=1,
                    dilation=dilation_size,
                    padding=(kernel_size - 1) * dilation_size,
                    dropout=dropout,
                )
            ]
        self.network = nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)

class TCNModel(nn.Module):
    def __init__(self, d_feat, num_channels=[64,64], kernel_size=5, dropout=0.5): # output_size, 
        super().__init__()
        self.d_feat = d_feat
        self.tcn = TemporalConvNet(d_feat, num_channels, kernel_size, dropout=dropout)
        # self.linear = nn.Linear(num_channels[-1], output_size=1)
        self.fc1 = nn.Linear(in_features=num_channels[-1], out_features=num_channels[-1])
        self.bn1 = nn.BatchNorm1d(num_channels[-1], affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)

    def forward(self, x):
        # x: [N, F*T]
        x = x.reshape(x.shape[0], self.d_feat, -1)
        # x = x.permute(0, 2, 1)  # [N, T, F]
        output = self.tcn(x)
        # res = self.linear(output[:, :, -1])
        # res = res.squeeze()
        # return output[:, :, -1], res
        res = self.fc1(output[:, :, -1])
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- HIST
class HISTModel(nn.Module):
    def __init__(self, d_feat=6, hidden_size=64, num_layers=2, dropout=0.1, base_model="GRU"):
        super().__init__()

        self.d_feat = d_feat
        self.hidden_size = hidden_size

        if base_model == "GRU":
            self.rnn = nn.GRU(
                input_size=d_feat,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
            )
        elif base_model == "LSTM":
            self.rnn = nn.LSTM(
                input_size=d_feat,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
            )
        else:
            raise ValueError("unknown base model name `%s`" % base_model)

        self.fc_es = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_es.weight)
        self.fc_is = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_is.weight)

        self.fc_es_middle = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_es_middle.weight)
        self.fc_is_middle = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_is_middle.weight)

        self.fc_es_fore = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_es_fore.weight)
        self.fc_is_fore = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_is_fore.weight)
        self.fc_indi_fore = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_indi_fore.weight)

        self.fc_es_back = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_es_back.weight)
        self.fc_is_back = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_is_back.weight)
        self.fc_indi = nn.Linear(hidden_size, hidden_size)
        torch.nn.init.xavier_uniform_(self.fc_indi.weight)

        self.leaky_relu = nn.LeakyReLU()
        self.softmax_s2t = torch.nn.Softmax(dim=0)
        self.softmax_t2s = torch.nn.Softmax(dim=1)

        self.fc_out_es = nn.Linear(hidden_size, 1)
        self.fc_out_is = nn.Linear(hidden_size, 1)
        self.fc_out_indi = nn.Linear(hidden_size, 1)
        # self.fc_out = nn.Linear(hidden_size, 1)
        self.fc1 = nn.Linear(in_features=self.hidden_size, out_features=self.hidden_size)
        self.bn1 = nn.BatchNorm1d(self.hidden_size, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)

    def cal_cos_similarity(self, x, y):  # the 2nd dimension of x and y are the same
        xy = x.mm(torch.t(y))
        x_norm = torch.sqrt(torch.sum(x * x, dim=1)).reshape(-1, 1)
        y_norm = torch.sqrt(torch.sum(y * y, dim=1)).reshape(-1, 1)
        cos_similarity = xy / (x_norm.mm(torch.t(y_norm)) + 1e-6)
        return cos_similarity

    def forward(self, x, concept_matrix):
        device = torch.device(torch.get_device(x))

        x_hidden = x.reshape(len(x), self.d_feat, -1)  # [N, F, T]
        x_hidden = x_hidden.permute(0, 2, 1)  # [N, T, F]
        x_hidden, _ = self.rnn(x_hidden)
        x_hidden = x_hidden[:, -1, :]

        # Predefined Concept Module

        stock_to_concept = concept_matrix

        stock_to_concept_sum = torch.sum(stock_to_concept, 0).reshape(1, -1).repeat(stock_to_concept.shape[0], 1)
        stock_to_concept_sum = stock_to_concept_sum.mul(concept_matrix)

        stock_to_concept_sum = stock_to_concept_sum + (
            torch.ones(stock_to_concept.shape[0], stock_to_concept.shape[1]).to(device)
        )
        stock_to_concept = stock_to_concept / stock_to_concept_sum
        hidden = torch.t(stock_to_concept).mm(x_hidden)

        hidden = hidden[hidden.sum(1) != 0]

        concept_to_stock = self.cal_cos_similarity(x_hidden, hidden)
        concept_to_stock = self.softmax_t2s(concept_to_stock)

        e_shared_info = concept_to_stock.mm(hidden)
        e_shared_info = self.fc_es(e_shared_info)

        e_shared_back = self.fc_es_back(e_shared_info)
        output_es = self.fc_es_fore(e_shared_info)
        output_es = self.leaky_relu(output_es)

        # Hidden Concept Module
        i_shared_info = x_hidden - e_shared_back
        hidden = i_shared_info
        i_stock_to_concept = self.cal_cos_similarity(i_shared_info, hidden)
        dim = i_stock_to_concept.shape[0]
        diag = i_stock_to_concept.diagonal(0)
        i_stock_to_concept = i_stock_to_concept * (torch.ones(dim, dim) - torch.eye(dim)).to(device)
        row = torch.linspace(0, dim - 1, dim).to(device).long()
        column = i_stock_to_concept.max(1)[1].long()
        value = i_stock_to_concept.max(1)[0]
        i_stock_to_concept[row, column] = 10
        i_stock_to_concept[i_stock_to_concept != 10] = 0
        i_stock_to_concept[row, column] = value
        i_stock_to_concept = i_stock_to_concept + torch.diag_embed((i_stock_to_concept.sum(0) != 0).float() * diag)
        hidden = torch.t(i_shared_info).mm(i_stock_to_concept).t()
        hidden = hidden[hidden.sum(1) != 0]

        i_concept_to_stock = self.cal_cos_similarity(i_shared_info, hidden)
        i_concept_to_stock = self.softmax_t2s(i_concept_to_stock)
        i_shared_info = i_concept_to_stock.mm(hidden)
        i_shared_info = self.fc_is(i_shared_info)

        i_shared_back = self.fc_is_back(i_shared_info)
        output_is = self.fc_is_fore(i_shared_info)
        output_is = self.leaky_relu(output_is)

        # Individual Information Module
        individual_info = x_hidden - e_shared_back - i_shared_back
        output_indi = individual_info
        output_indi = self.fc_indi(output_indi)
        output_indi = self.leaky_relu(output_indi)

        # Stock Trend Prediction
        all_info = output_es + output_is + output_indi
        # pred_all = self.fc_out(all_info).squeeze()
        # return pred_all
        res = self.fc1(all_info)
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res
        
#-- Transformer
class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=1000):
        super(PositionalEncoding, self).__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer("pe", pe)

    def forward(self, x):
        # [T, N, F]
        return x + self.pe[: x.size(0), :]

class TransformerModel(nn.Module):
    def __init__(self, d_feat=6, d_model=8, nhead=4, num_layers=2, dropout=0.5, device=None):
        super(TransformerModel, self).__init__()
        self.feature_layer = nn.Linear(d_feat, d_model)
        self.pos_encoder = PositionalEncoding(d_model)
        self.encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, dropout=dropout)
        self.transformer_encoder = nn.TransformerEncoder(self.encoder_layer, num_layers=num_layers)
        # self.decoder_layer = nn.Linear(d_model, 1)
        self.fc1 = nn.Linear(in_features=d_model, out_features=d_model)
        self.bn1 = nn.BatchNorm1d(d_model, affine=False)
        self.bn2 = nn.BatchNorm1d(1, affine=False)
        self.device = device
        self.d_feat = d_feat

    def forward(self, src):
        # src [N, F*T] --> [N, T, F]
        src = src.reshape(len(src), self.d_feat, -1).permute(0, 2, 1)
        src = self.feature_layer(src)

        # src [N, T, F] --> [T, N, F], [60, 512, 8]
        src = src.transpose(1, 0)  # not batch first

        mask = None

        src = self.pos_encoder(src)
        output = self.transformer_encoder(src, mask)  # [60, 512, 8]

        # [T, N, F] --> [N, T*F]
        # output = self.decoder_layer(output.transpose(1, 0)[:, -1, :])  # [512, 1]
        # return output.squeeze()
        res = self.fc1(output.transpose(1, 0)[:, -1, :])
        res = self.bn1(res)
        x = torch.mean(res, dim=1, keepdim=True)
        x = self.bn2(x)
        return x, res

#-- xLSTM
#-- entry
def seed_torch(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)  
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    return None

def setup_nn_model(model_id, input_size, device):
    if model_id=='gru':
        main_model = copy.deepcopy(GRUModel(d_feat=input_size)).to(device)
    elif model_id=='bigru':
        main_model = copy.deepcopy(BiGRUModel(d_feat=input_size)).to(device)
    elif model_id=='agru':
        main_model = copy.deepcopy(AGRUModel(d_feat=input_size)).to(device)
    elif model_id=='agrus':
        main_model = copy.deepcopy(AGRUSModel(d_feat=input_size)).to(device)
    elif model_id=='agrum':
        main_model = copy.deepcopy(AGRUMModel(d_feat=input_size)).to(device)
    elif model_id=='resgru':
        main_model = copy.deepcopy(ResGRUModel(d_feat=input_size)).to(device)
    elif model_id=='gat': # slow, but useful
        main_model = copy.deepcopy(GATModel(d_feat=input_size)).to(device)
    elif model_id=='lstm': # slow and not useful
        main_model = copy.deepcopy(LSTMModel(d_feat=input_size)).to(device)
    elif model_id=='transformer':
        main_model = copy.deepcopy(TransformerModel(d_feat=input_size)).to(device)
    elif model_id=='tcn':
        main_model = TCNModel(d_feat=input_size).to(device)
    elif model_id=='hist':
        main_model = copy.deepcopy(HISTModel(d_feat=input_size)).to(device)
    else:
        raise Exception('unknown model_id: '+model_id)
    return main_model

