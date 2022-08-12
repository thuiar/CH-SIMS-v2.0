"""
paper: Tensor Fusion Network for Multimodal Sentiment Analysis
ref: https://github.com/A2Zadeh/TensorFusionNetwork
"""
from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.parameter import Parameter
from torch.nn.init import xavier_uniform, xavier_normal, orthogonal
from models.subNets.BertTextEncoder import BertTextEncoder
import numpy as np
__all__ = ['MTFN']

class SubNet(nn.Module):
    '''
    The subnetwork that is used in TFN for video and audio in the pre-fusion stage
    '''

    def __init__(self, in_size, hidden_size, dropout):
        '''
        Args:
            in_size: input dimension
            hidden_size: hidden layer dimension
            dropout: dropout probability
        Output:
            (return value in forward) a tensor of shape (batch_size, hidden_size)
        '''
        super(SubNet, self).__init__()
        self.norm = nn.BatchNorm1d(in_size)
        self.drop = nn.Dropout(p=dropout)
        self.linear_1 = nn.Linear(in_size, hidden_size)
        self.linear_2 = nn.Linear(hidden_size, hidden_size)
        self.linear_3 = nn.Linear(hidden_size, hidden_size)

    def forward(self, x):
        '''
        Args:
            x: tensor of shape (batch_size, in_size)
        '''
        normed = self.norm(x)
        dropped = self.drop(normed)
        y_1 = F.relu(self.linear_1(dropped))
        y_2 = F.relu(self.linear_2(y_1))
        y_3 = F.relu(self.linear_3(y_2))

        return y_3


class TextSubNet(nn.Module):
    '''
    The LSTM-based subnetwork that is used in TFN for text
    '''

    def __init__(self, in_size, hidden_size, out_size, num_layers=1, dropout=0.2, bidirectional=False):
        '''
        Args:
            in_size: input dimension
            hidden_size: hidden layer dimension
            num_layers: specify the number of layers of LSTMs.
            dropout: dropout probability
            bidirectional: specify usage of bidirectional LSTM
        Output:
            (return value in forward) a tensor of shape (batch_size, out_size)
        '''
        super(TextSubNet, self).__init__()
        if num_layers == 1:
            dropout = 0.0
        self.rnn = nn.LSTM(in_size, hidden_size, num_layers=num_layers, dropout=dropout, bidirectional=bidirectional, batch_first=True)
        self.dropout = nn.Dropout(dropout)
        self.linear_1 = nn.Linear(hidden_size, out_size)

    def forward(self, x):
        '''
        Args:
            x: tensor of shape (batch_size, sequence_len, in_size)
        '''
        _, final_states = self.rnn(x)
        h = self.dropout(final_states[0].squeeze())
        y_1 = self.linear_1(h)
        return y_1


class V0(nn.Module):
    def __init__(self, args):
        super(V0, self).__init__()
        # dimensions are specified in the order of audio, video and text
        self.text_in, self.audio_in, self.video_in = args.feature_dims
        self.text_hidden, self.audio_hidden, self.video_hidden = args.hidden_dims
        self.text_model = BertTextEncoder(language=args.language, use_finetune=args.use_bert_finetune)
        self.tliner = nn.Linear(self.text_in, args.text_out)
        self.text_out= args.text_out

        self.audio_prob, self.video_prob, self.text_prob = args.dropouts
        self.post_text_prob, self.post_audio_prob, self.post_video_prob, self.post_fusion_prob = args.post_dropouts

        self.post_fusion_dim = args.post_fusion_dim
        self.post_text_dim = args.post_text_dim
        self.post_audio_dim = args.post_audio_dim
        self.post_video_dim = args.post_video_dim

        self.output_dim = args.num_classes if args.train_mode == "classification" else 1

        # define the pre-fusion subnetworks
        self.audio_model = SubNet(self.audio_in, self.audio_hidden, self.audio_prob)
        self.video_model = SubNet(self.video_in, self.video_hidden, self.video_prob)

        # define the post_fusion layers
        self.post_fusion_dropout = nn.Dropout(p=self.post_fusion_prob)
        self.post_fusion_layer_1 = nn.Linear((self.text_out + 1) * (self.video_hidden + 1) * (self.audio_hidden + 1), self.post_fusion_dim)
        self.post_fusion_layer_2 = nn.Linear(self.post_fusion_dim, self.post_fusion_dim)
        self.post_fusion_layer_3 = nn.Linear(self.post_fusion_dim, self.output_dim)

        # define the classify layer for text
        self.post_text_dropout = nn.Dropout(p=self.post_text_prob)
        self.post_text_layer_1 = nn.Linear(self.text_out, self.post_text_dim)
        self.post_text_layer_2 = nn.Linear(self.post_text_dim, self.post_text_dim)
        self.post_text_layer_3 = nn.Linear(self.post_text_dim, self.output_dim)

        # define the classify layer for audio
        self.post_audio_dropout = nn.Dropout(p=self.post_audio_prob)
        self.post_audio_layer_1 = nn.Linear(self.audio_hidden, self.post_audio_dim)
        self.post_audio_layer_2 = nn.Linear(self.post_audio_dim, self.post_audio_dim)
        self.post_audio_layer_3 = nn.Linear(self.post_audio_dim, self.output_dim)

        # define the classify layer for video
        self.post_video_dropout = nn.Dropout(p=self.post_video_prob)
        self.post_video_layer_1 = nn.Linear(self.video_hidden, self.post_video_dim)
        self.post_video_layer_2 = nn.Linear(self.post_video_dim, self.post_video_dim)
        self.post_video_layer_3 = nn.Linear(self.post_video_dim, self.output_dim)

        # in TFN we are doing a regression with constrained output range: (-3, 3), hence we'll apply sigmoid to output
        # shrink it to (0, 1), and scale\shift it back to range (-3, 3)
        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

        # alpha = torch.rand(1,1)
        # self.alpha = nn.Parameter(alpha)
        # alpha2 = torch.rand(1,1)
        # self.alpha2 = nn.Parameter(alpha2)
        # alpha3 = torch.rand(1,1)
        # self.alpha3 = nn.Parameter(alpha3)

    def forward(self, text_x, audio_x, video_x):
        '''
        Args:
            audio_x: tensor of shape (batch_size, audio_in)
            video_x: tensor of shape (batch_size, video_in)
            text_x: tensor of shape (batch_size, sequence_len, text_in)
        '''
        text_x = self.text_model(text_x)[:,0,:]
        text_h = self.tliner(text_x)

        audio_h = self.audio_model(audio_x.squeeze(1))
        video_h = self.video_model(video_x.squeeze(1))

        # text
        x_t = self.post_text_dropout(text_h)
        x_t = F.relu(self.post_text_layer_1(x_t), inplace=True)
        x_t = F.relu(self.post_text_layer_2(x_t), inplace=True)
        output_text = self.post_text_layer_3(x_t)
        # audio
        x_a = self.post_audio_dropout(audio_h)
        x_a = F.relu(self.post_audio_layer_1(x_a), inplace=True)
        x_a = F.relu(self.post_audio_layer_2(x_a), inplace=True)
        output_audio = self.post_audio_layer_3(x_a)
        # video
        x_v = self.post_video_dropout(video_h)
        x_v = F.relu(self.post_video_layer_1(x_v), inplace=True)
        x_v = F.relu(self.post_video_layer_2(x_v), inplace=True)
        output_video = self.post_video_layer_3(x_v)

        batch_size = audio_h.data.shape[0]
        # next we perform "tensor fusion", which is essentially appending 1s to the tensors and take Kronecker product
        add_one = torch.ones(size=[batch_size, 1], requires_grad=False).type_as(audio_h).to(text_x.device)
        _audio_h = torch.cat((add_one, audio_h), dim=1)
        _video_h = torch.cat((add_one, video_h), dim=1)
        _text_h = torch.cat((add_one, text_h), dim=1)

        # _audio_h has shape (batch_size, audio_in + 1), _video_h has shape (batch_size, _video_in + 1)
        # we want to perform outer product between the two batch, hence we unsqueenze them to get
        # (batch_size, audio_in + 1, 1) X (batch_size, 1, video_in + 1)
        # fusion_tensor will have shape (batch_size, audio_in + 1, video_in + 1)
        fusion_tensor = torch.bmm(_audio_h.unsqueeze(2), _video_h.unsqueeze(1))
        
        # next we do kronecker product between fusion_tensor and _text_h. This is even trickier
        # we have to reshape the fusion tensor during the computation
        # in the end we don't keep the 3-D tensor, instead we flatten it
        fusion_tensor = fusion_tensor.view(-1, (self.audio_hidden + 1) * (self.video_hidden + 1), 1)
        fusion_tensor = torch.bmm(fusion_tensor, _text_h.unsqueeze(1)).view(batch_size, -1)

        post_fusion_dropped = self.post_fusion_dropout(fusion_tensor)
        post_fusion_y_1 = F.relu(self.post_fusion_layer_1(post_fusion_dropped), inplace=True)
        post_fusion_y_2 = F.relu(self.post_fusion_layer_2(post_fusion_y_1), inplace=True)
        output_fusion = self.post_fusion_layer_3(post_fusion_y_2)
        if self.output_dim == 1:
            output_fusion = torch.sigmoid(output_fusion)
            output_fusion = output_fusion * self.output_range + self.output_shift

        res = {
            'Feature_t': text_h,
            'Feature_a': audio_h,
            'Feature_v': video_h,
            'Feature_f': fusion_tensor,
            'M': output_fusion,
            'T': output_text,
            'A': output_audio,
            'V': output_video
        }

        return res
class AuViSubNet(nn.Module):
    def __init__(self, in_size, hidden_size, out_size, num_layers=1, dropout=0.2, bidirectional=False):
        '''
        Args:
            in_size: input dimension
            hidden_size: hidden layer dimension
            num_layers: specify the number of layers of LSTMs.
            dropout: dropout probability
            bidirectional: specify usage of bidirectional LSTM
        Output:
            (return value in forward) a tensor of shape (batch_size, out_size)
        '''
        super(AuViSubNet, self).__init__()
        self.rnn = nn.LSTM(in_size, hidden_size, num_layers=num_layers, dropout=dropout, bidirectional=bidirectional, batch_first=True)
        self.dropout = nn.Dropout(dropout)
        self.linear_1 = nn.Linear(hidden_size, out_size)

    def forward(self, x):
        '''
        x: (batch_size, sequence_len, in_size)
        '''
        # packed_sequence = pack_padded_sequence(x, lengths, batch_first=True, enforce_sorted=False)
        _, final_states = self.rnn(x)
        h = self.dropout(final_states[0].squeeze())
        y_1 = self.linear_1(h)
        return y_1