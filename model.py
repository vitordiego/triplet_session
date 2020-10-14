from typing import Dict, Any, List, Tuple, Union
import os
import luigi
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchbearer import Trial
from mars_gym.meta_config import ProjectConfig
from mars_gym.model.abstract import RecommenderModule
from mars_gym.model.bandit import BanditPolicy
from mars_gym.torch.init import lecun_normal_init

from numpy.random.mtrand import RandomState
import random

from typing import Optional, Callable, Tuple

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


class LinearWeightedAvg(nn.Module):
    def __init__(self, n_inputs):
        super(LinearWeightedAvg, self).__init__()
        self.weights = nn.ParameterList([nn.Parameter(torch.randn(1)) for i in range(n_inputs)])

    def forward(self, input):
        res = 0
        for emb_idx, emb in enumerate(input):
            res += emb * self.weights[emb_idx]
        return res
        
class EmbeddingDropout(nn.Module):
    def __init__(self, p: float = 0.5,) -> None:
        super().__init__()
        if p < 0 or p > 1:
            raise ValueError(
                "dropout probability has to be between 0 and 1, " "but got {}".format(p)
            )
        self.p = p

    # def forward(self, *embs: torch.Tensor) -> Tuple[torch.Tensor, ...]:
    #     if self.training and self.p:
    #         mask = {}
    #         for emb in embs:
    #             mask[emb] = torch.bernoulli(
    #                 torch.tensor(1 - self.p, device=embs[0].device).expand(
    #                     *embs[0].shape
    #                 )
    #             ) / (1 - self.p)
            
    #         return tuple(emb * mask[emb] for emb in embs)
    #     return tuple(embs)

    def forward(self, *embs: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        if self.training and self.p:
            mask = {}
            mask = torch.bernoulli(
                torch.tensor(1 - self.p, device=embs[0].device).expand(
                    *embs[0].shape
                )
            ) / (1 - self.p)
            
            return tuple(emb * mask for emb in embs)
        return tuple(embs)

class SimpleLinearModel(RecommenderModule):
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int,
        path_item_embedding: str
    ):
        super().__init__(project_config, index_mapping)
        self.path_item_embedding = path_item_embedding

        if self.path_item_embedding:
            weights = np.loadtxt(self.path_item_embedding)
            self.item_embeddings = nn.Embedding.from_pretrained(torch.from_numpy(weights).float(),freeze=False)
            #self.item_embeddings.weight.requires_grad = False       
        else:
            self.item_embeddings = nn.Embedding(self._n_items, n_factors)

        self.user_embeddings = nn.Embedding(self._n_users, n_factors)
        self.dayofweek_embeddings = nn.Embedding(7, n_factors)

        num_dense = n_factors * (1 + 10)

        self.dense = nn.Sequential(
            nn.Linear(num_dense, int(num_dense / 2)),
            nn.SELU(),
            nn.Linear(int(num_dense / 2), 1),
        )

        self.weight_init = lecun_normal_init
        self.apply(self.init_weights)

    def init_weights(self, module: nn.Module):
        if type(module) == nn.Linear:
            self.weight_init(module.weight)
            module.bias.data.fill_(0.1)
            
    def flatten(self, input):
        return input.view(input.size(0), -1)

    def forward(self, session_ids, item_ids, item_history_ids):
        # Item emb
        item_emb = self.item_embeddings(item_ids)

        # Item History embs
        interaction_item_emb = self.flatten(self.item_embeddings(item_history_ids))

        ## DayofWeek Emb
        #dayofweek_emb = self.dayofweek_embeddings(dayofweek.long())

        x = torch.cat(
            (item_emb, interaction_item_emb),
            dim=1,
        )

        out = torch.sigmoid(self.dense(x))
        return out

class GRURecModel(RecommenderModule):
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int,
        path_item_embedding: str,
        dropout: float,
        freeze_embedding: bool
    ):
        super().__init__(project_config, index_mapping)
        self.path_item_embedding = path_item_embedding
        self.dropout = dropout
        self.hidden_size = 100 
        self.n_layers = 1

        if self.path_item_embedding:
            weights = np.loadtxt(self.path_item_embedding)
            self.item_embeddings = nn.Embedding.from_pretrained(torch.from_numpy(weights).float(),freeze=freeze_embedding)
            #self.item_embeddings.weight.requires_grad = False       
        else:
            self.item_embeddings = nn.Embedding(self._n_items, n_factors)

        self.gru = nn.GRU(n_factors, self.hidden_size, self.n_layers, dropout=self.dropout)
        self.out = nn.Linear(self.hidden_size, self._n_items)
        self.sf  = nn.Softmax()

        self.weight_init = lecun_normal_init
        self.apply(self.init_weights)

    def init_weights(self, module: nn.Module):
        if type(module) == nn.Linear:
            self.weight_init(module.weight)
            module.bias.data.fill_(0.1)
            
    def flatten(self, input):
        return input.view(input.size(0), -1)

    def normalize(self, x: torch.Tensor, dim: int = 1) -> torch.Tensor:
        if self.use_normalize:
            x = F.normalize(x, p=2, dim=dim)
        return x

    def forward(self, session_ids, item_ids, item_history_ids):
        embs = self.item_embeddings(item_history_ids)
        
        output, hidden = self.gru(embs)
        #output = output.view(-1, output.size(2))  #(B,H)
        
        #out    = torch.softmax(self.out(output[:,-1]), dim=1)
        out    = self.out(output[:,-1])
        return out

    def recommendation_score(self, session_ids, item_ids, item_history_ids):
        
        scores = self.forward(session_ids, item_ids, item_history_ids)
        scores = scores[torch.arange(scores.size(0)),item_ids]

        return scores

class NARMModel(RecommenderModule):
    '''
    https://github.com/Wang-Shuo/Neural-Attentive-Session-Based-Recommendation-PyTorch.git
    '''
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int,
        n_layers: int,
        hidden_size: int,
    ):
        super().__init__(project_config, index_mapping)

        self.hidden_size = hidden_size
        self.n_layers = n_layers
        self.embedding_dim = n_factors
        self.emb = nn.Embedding(self._n_items, self.embedding_dim, padding_idx = 0)
        self.emb_dropout = nn.Dropout(0.25)
        self.gru = nn.GRU(self.embedding_dim, self.hidden_size, self.n_layers)
        self.a_1 = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.a_2 = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.v_t = nn.Linear(self.hidden_size, 1, bias=False)
        self.ct_dropout = nn.Dropout(0.5)
        self.b = nn.Linear(self.embedding_dim, 2 * self.hidden_size, bias=False)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def forward(self, session_ids, item_ids, item_history_ids):
        device = item_ids.device
        seq    = item_history_ids.permute(1,0)  #TODO

        hidden = self.init_hidden(seq.size(1)).to(device)
        embs = self.emb_dropout(self.emb(seq))
        #embs = pack_padded_sequence(embs, lengths)
        gru_out, hidden = self.gru(embs, hidden)
        #gru_out, lengths = pad_packed_sequence(gru_out)

        # fetch the last hidden state of last timestamp
        ht = hidden[-1]
        gru_out = gru_out.permute(1, 0, 2)

        c_global = ht
        q1 = self.a_1(gru_out.contiguous().view(-1, self.hidden_size)).view(gru_out.size())  
        q2 = self.a_2(ht)

        mask = torch.where(seq.permute(1, 0) > 0, torch.tensor([1.], device = device), torch.tensor([0.], device = device))

        q2_expand = q2.unsqueeze(1).expand_as(q1)
        q2_masked = mask.unsqueeze(2).expand_as(q1) * q2_expand

        alpha = self.v_t(torch.sigmoid(q1 + q2_masked).view(-1, self.hidden_size)).view(mask.size())
        c_local = torch.sum(alpha.unsqueeze(2).expand_as(gru_out) * gru_out, 1)

        c_t = torch.cat([c_local, c_global], 1)
        c_t = self.ct_dropout(c_t)
        
        item_embs = self.emb(torch.arange(self._n_items).to(device).long())
        scores = torch.matmul(c_t, self.b(item_embs).permute(1, 0))

        return scores

    def recommendation_score(self, session_ids, item_ids, item_history_ids):
        
        scores = self.forward(session_ids, item_ids, item_history_ids)
        scores = scores[torch.arange(scores.size(0)),item_ids]

        return scores

    def init_hidden(self, batch_size):
        return torch.zeros((self.n_layers, batch_size, self.hidden_size), requires_grad=True)
        
class MatrixFactorizationModel(RecommenderModule):
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int,
        path_item_embedding: str,
        dropout: float,
        freeze_embedding: bool,
        hist_size: int,
        weight_decay: float
    ):
        super().__init__(project_config, index_mapping)
        self.path_item_embedding = path_item_embedding
        self.hist_size = hist_size
        self.weight_decay = weight_decay # 0.025#1e-5

        self.hist_embeddings = nn.Embedding(self._n_items, n_factors)

        if self.path_item_embedding:
            weights = np.loadtxt(self.path_item_embedding)
            self.item_embeddings = nn.Embedding.from_pretrained(torch.from_numpy(weights).float(),freeze=freeze_embedding)
        else:
            self.item_embeddings = nn.Embedding(self._n_items, n_factors)

        self.linear_w_emb = nn.Linear(n_factors*self.hist_size, n_factors)
        self.weight_emb =  nn.Parameter(torch.randn(self.hist_size, 1))
        self.use_normalize = True
        self.weight_init = lecun_normal_init

        self.apply(self.init_weights)

    def init_weights(self, module: nn.Module):
        if type(module) == nn.Linear:
            self.weight_init(module.weight)
            module.bias.data.fill_(0.1)
            
    def flatten(self, input):
        return input.view(input.size(0), -1)

    def normalize(self, x: torch.Tensor, dim: int = 1) -> torch.Tensor:
        if self.use_normalize:
            x = F.normalize(x, p=2, dim=dim)
        return x

    def forward(self, session_ids = None, item_ids = None, item_history_ids = None, negative_item_ids = None):
        # Item emb
        item_emb = self.normalize(self.item_embeddings(item_ids))

        # Item History embs
        hist_emb = self.normalize(self.hist_embeddings(item_history_ids), 2)

        #hist_emb_mean = torch.stack([self.linear_w_avg(emb) for emb in hist_emb], dim=0)
        #hist_mean_emb = hist_emb.mean(1)
        #hist_mean_emb = self.linear_w_emb(self.flatten(hist_emb))

        hist_mean_emb = torch.matmul(hist_emb.permute(0, 2, 1), self.weight_emb)
        hist_mean_emb = self.flatten(hist_mean_emb)

        dot_p = (item_emb * hist_mean_emb).sum(1)

        item_neg_emb = self.normalize(self.item_embeddings(negative_item_ids))

        dot_n = (item_neg_emb * hist_mean_emb).sum(1)

        diff  = dot_p - dot_n
        log_prob = F.logsigmoid(diff).sum()
        regularization = self.weight_decay * (hist_mean_emb.norm(dim=0).pow(2).sum() + item_emb.norm(dim=0).pow(2).sum())
        return -log_prob + regularization        

    def recommendation_score(self, session_ids, item_ids, item_history_ids):
        # Item emb
        item_emb = self.normalize(self.item_embeddings(item_ids))

        # Item History embs
        hist_emb = self.normalize(self.hist_embeddings(item_history_ids), 2)

        hist_mean_emb = torch.matmul(hist_emb.permute(0, 2, 1), self.weight_emb)
        hist_mean_emb = self.flatten(hist_mean_emb)

        dot_p = (item_emb * hist_mean_emb).sum(1)
        
        scores = torch.sigmoid(dot_p)
        
        return scores

class DotModel(RecommenderModule):
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int,
        path_item_embedding: str,
        dropout: float,
        freeze_embedding: bool
    ):
        super().__init__(project_config, index_mapping)
        self.path_item_embedding = path_item_embedding

        if self.path_item_embedding:
            weights = np.loadtxt(self.path_item_embedding)
            self.item_embeddings = nn.Embedding.from_pretrained(torch.from_numpy(weights).float(),freeze=freeze_embedding)
            #self.item_embeddings.weight.requires_grad = False       
        else:
            self.item_embeddings = nn.Embedding(self._n_items, n_factors)

        num_dense = 10
        
        self.dense = nn.Sequential(
            nn.Dropout(p=dropout),
            nn.Linear(num_dense, int(num_dense / 2)),
            nn.SELU(),
            nn.Linear(int(num_dense / 2), 1),
        )
        self.use_normalize = True
        self.weight_init = lecun_normal_init
        self.apply(self.init_weights)

    def init_weights(self, module: nn.Module):
        if type(module) == nn.Linear:
            self.weight_init(module.weight)
            module.bias.data.fill_(0.1)
            
    def flatten(self, input):
        return input.view(input.size(0), -1)


    def normalize(self, x: torch.Tensor, dim: int = 1) -> torch.Tensor:
        if self.use_normalize:
            x = F.normalize(x, p=2, dim=dim)
        return x

    def forward(self, session_ids, item_ids, item_history_ids):
        # Item emb
        item_emb = self.normalize(self.item_embeddings(item_ids))

        # Item History embs
        hist_emb = self.normalize(self.item_embeddings(item_history_ids))
        
        dot_prod = torch.matmul(item_emb.unsqueeze(1), hist_emb.permute(0, 2, 1))

        dot_prod = self.flatten(dot_prod)
        
        ## DayofWeek Emb
        #dayofweek_emb = self.dayofweek_embeddings(dayofweek.long())

        out = torch.sigmoid(self.dense(dot_prod))

        return out

class TripletNet(RecommenderModule):
    def __init__(
        self,
        project_config: ProjectConfig,
        index_mapping: Dict[str, Dict[Any, int]],
        n_factors: int, use_normalize: bool,
        dropout: float,
        negative_random: float
    ):

        super().__init__(project_config, index_mapping)

        self.use_normalize   = use_normalize
        self.item_embeddings = nn.Embedding(self._n_items, n_factors)
        self.negative_random = negative_random
        self.dropout = dropout
        self.dropout_emb = EmbeddingDropout(dropout)
        self.dropout_emb2 = nn.Dropout(p=dropout)
        #self.weight_init = lecun_normal_init
        self.init_weights()
        #self.apply(self.init_weights)
        
    def embedded_dropout(self, embed, words, dropout=0.1):
        mask = embed.weight.data.new_empty((embed.weight.size(0), 1)).bernoulli_(1 - dropout).expand_as(embed.weight) / (1 - dropout)
        masked_embed_weight = mask * embed.weight
        return torch.nn.functional.embedding(words, masked_embed_weight)
                
    def init_weights(self):
        initrange = 0.1
        self.item_embeddings.weight.data.uniform_(-initrange, initrange)

    def normalize(self, x: torch.Tensor, dim: int = 1) -> torch.Tensor:
        if self.use_normalize:
            x = F.normalize(x, p=2, dim=dim)
        return x

    def get_harder_negative(self, item_ids: torch.Tensor, 
                            positive_item_ids: torch.Tensor, 
                            negative_item_list_idx: List[torch.Tensor]) -> Tuple[torch.Tensor]:

        batch_size = item_ids.size(0)

        anchors    = self.normalize(self.item_embeddings(item_ids.long()))  # (B, E)

        all_negative_items_embedding = self.normalize(
            self.item_embeddings(negative_item_list_idx), dim=2)  # (B, 100, E)

        distances_between_archor_and_negatives = (
            anchors.reshape(batch_size, 1, self.item_embeddings.embedding_dim)
            * all_negative_items_embedding
        ).sum(
            2
        )  # (B, 100)

        hardest_negative_items = torch.argmax(
            distances_between_archor_and_negatives, dim=1
        )  # (B,)

        # negatives = all_negative_items_embedding[
        #     torch.arange(0, batch_size, device=item_ids.device), hardest_negative_items
        # ]

        negative_idx = negative_item_list_idx[
            torch.arange(0, batch_size, device=item_ids.device), hardest_negative_items
        ]
        return negative_idx

    def select_negative_item_emb(self, item_ids: torch.Tensor, positive_item_ids: torch.Tensor,
                        negative_list_idx: List[torch.Tensor] = None):

        if random.random() < self.negative_random:
            negative_item_idx = negative_list_idx[:,0]
            #negative_item_emb = self.normalize(self.item_embeddings(negative_item_idx.long()))
        else:
            negative_item_idx = self.get_harder_negative(item_ids, positive_item_ids, negative_list_idx)
        
        return negative_item_idx

    def similarity(self, itemA, itemB):
        return torch.cosine_similarity(itemA, itemB)

    def forward(self, item_ids: torch.Tensor, 
                      positive_item_ids: torch.Tensor,
                      negative_list_idx: List[torch.Tensor] = None) -> Union[Tuple[torch.Tensor, torch.Tensor, torch.Tensor], torch.Tensor]:
        
        #arch_item_emb = self.embedded_dropout(self.item_embeddings, item_ids.long(), dropout=self.dropout if self.training else 0)
        
        arch_item_emb = self.normalize(self.item_embeddings(item_ids.long()))

        #arch_item_emb = self.normalize(self.item_embeddings(item_ids.long()))
        #positive_item_emb = self.embedded_dropout(self.item_embeddings, positive_item_ids.long(), dropout=self.dropout if self.training else 0)
        positive_item_emb = self.item_embeddings(positive_item_ids.long())
        positive_item_emb = self.normalize(positive_item_emb)
        
        #dot_arch_pos = F.sigmoid((arch_item_emb * positive_item_emb).sum(1))
        dot_arch_pos = (arch_item_emb * positive_item_emb).sum(1)

        if negative_list_idx is None:
            self.similarity(arch_item_emb, positive_item_emb)

        negative_item_ids = self.select_negative_item_emb(item_ids, positive_item_ids, negative_list_idx)
        
        #negative_item_emb = self.embedded_dropout(self.item_embeddings, negative_item_ids.long(), dropout=self.dropout if self.training else 0)
        negative_item_emb = self.item_embeddings(negative_item_ids.long())
        negative_item_emb = self.normalize(negative_item_emb)
        
        return self.dropout_emb(arch_item_emb, positive_item_emb, negative_item_emb)
        #return self.dropout_emb2(arch_item_emb), self.dropout_emb2(positive_item_emb), self.dropout_emb2(negative_item_emb)
        
        #return arch_item_emb, positive_item_emb, negative_item_emb, dot_arch_pos

