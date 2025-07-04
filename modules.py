import torch
import torch.nn.functional as F
from torch import nn
from typing import Optional, Tuple, List, Dict, Any, Callable
from numba import njit, int32, jit
from numba.typed import List
from numba.core.errors import NumbaDeprecationWarning, NumbaPendingDeprecationWarning
import warnings#

warnings.simplefilter('ignore', category=NumbaDeprecationWarning)
warnings.simplefilter('ignore', category=NumbaPendingDeprecationWarning)
import numpy as  np

@torch.jit.script
def normfun(x, mean, std):
    return (x-mean) / (std+0.001), x.mean(0), x.std(0)

@torch.jit.script
def swiglu(x):
    x, gate = x.chunk(2, dim=-1)
    return F.silu(gate) * x

@torch.jit.script
def feature_transform(feat:torch.Tensor, n_sin:int, sigma: float=6):
    transformed = torch.empty(feat.shape[0],2,n_sin,feat.shape[1])
    for i in range(1,1+n_sin):
        freqs = 2*torch.pi* sigma ** (i/n_sin)
        transformed[:,0,i-1] = torch.sin(feat * freqs)
        transformed[:,1,i-1] = torch.cos(feat * freqs)
        #encs.append(transformed_sin)
        #encs.append(transformed_cos)
    transformed = transformed.reshape(feat.shape[0],-1)
    transformed = torch.cat([transformed,feat],-1)
    return transformed

class Feature_Transform(nn.Module):
    def __init__(self, n_sin,sigma) -> None:
        super().__init__()
        self.n_sin=n_sin
        self.sigma=sigma
    def forward(self,x):
        return feature_transform(x,self.n_sin,self.sigma)

class SlowNorm(nn.Module):
    def __init__(self,features,factor=0.05):
        super().__init__()
        self.register_buffer("std",torch.ones(features))
        #self.mean = torch.zeros(size)
        self.register_buffer("mean",torch.zeros(features))
        self.factor = factor
    def forward(self,x):
        x, mu_new, std_new = normfun(x,self.mean,self.std)
        with torch.no_grad():
            if self.training:
                self.mean = (1-self.factor)*self.mean + self.factor*mu_new
                self.std = (1-self.factor)*self.std + self.factor*std_new
        return x


class FeatureEmbedder(nn.Module):
    def __init__(self, feature_in, feature_embed_out: int ,n_layers:int = 1, scale=0.5):
        super().__init__()
        self.embd = nn.Sequential(
            #nn.BatchNorm1d(feature_in,affine=False),
            SlowNorm(feature_in),
            #Feature_Transform(5,6),
            nn.Linear(feature_in,#*(2*5+1), 
                      feature_embed_out),

            )
        layers = []
        
        for i in range(n_layers):
            layers.append(
                nn.Sequential(
                    nn.Linear(feature_embed_out,#*(2*5+1), 
                            feature_embed_out),
                    nn.LeakyReLU(),
                    )
                )
        self.layers = nn.ModuleList(layers)
        #self.weighters = nn.Parameter(torch.zeros(n_layers))
        self.norm = nn.LayerNorm(feature_embed_out,elementwise_affine=False)
        self.scale = scale

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # since at the beginning each node is independent,
        # we can still use batchnorm.
        # we can't do this later in the combiner due to
        # the correlations between nodes
        x = F.leaky_relu(self.embd(x))
        for idx,m in enumerate(self.layers):
            x = F.leaky_relu(m(x)) + x
        x = self.norm(x)
        return x*self.scale

def mapping(probs, steps):
    s = torch.softmax(probs, -1)
    i = torch.argmax(s,-1,keepdim=True)
    i1 = torch.minimum(i+1,torch.ones_like(i)*(s.shape[-1]-2))
    i2 = torch.maximum(i-1,torch.ones_like(i))

    v0 = torch.gather(s,1,i)
    v1 = torch.gather(s,1,i1)
    v2 = torch.gather(s,1,i2)
    vout = torch.where(v1>v2, v1, v2)
    iout = torch.where(v1>v2, i1, i2)
    weight = torch.softmax(torch.cat([vout,v0],-1),-1)
    values = torch.cat([iout,steps[i]],-1)
    weighted_sum =weight*values
    return weighted_sum.sum(-1,keepdim=True)

@njit(parallel=True)
def transform_ind(label ,neighbors):
    neighs = np.zeros(neighbors.shape)
    for i in range(neighbors.shape[0]):
        #for j in range(neighbors.shape[1]):
            # np.argwhere(label==neighbors[i,j])[0][0]
        neighs[i,0] = label.index(neighbors[i,0])
        neighs[i,1] = label.index(neighbors[i,1])
    return neighs


def find_neighbor_indices(label:torch.LongTensor, neighbors:torch.LongTensor):
    neighbors = neighbors.squeeze(1)
    #print(neighbors.shape)

    #neighbor_indices = torch.ones(len(neighbors),2,).long()*(-1)
    label = label.tolist()
    neighbor_indices = transform_ind(label, neighbors.numpy())
    # print("neighbors",neighbors)
    # print("neighbor ind", neighbor_indices)
    # print("labels", label)
    return torch.LongTensor(neighbor_indices)

@torch.no_grad()
def init(x : nn.Module):
    if type(x) ==  nn.Linear:
        torch.nn.init.orthogonal_(x.weight,0.01)

@torch.no_grad()
def init_ortho(x : nn.Module):
    if type(x) ==  nn.Linear:
        torch.nn.init.orthogonal_(x.weight,)
        if x.bias is not None:
            torch.nn.init.constant_(x.bias, 0)


class CombineEmbedder(nn.Module):
    def __init__(self,feat_emb_sz:int, node_emb_sz: int, scale_features = 0.5, depth = 2,n_layers=1):
        super().__init__()
        self.node_emb_sz = node_emb_sz
        self.feat_emb_sz = feat_emb_sz
        #self.embd = nn.Sequential(nn.Linear(feature_emb_in+2*node_emb_sz, node_emb_sz),
        #                          nn.LeakyReLU(),
        #                          nn.BatchNorm1d(node_emb_sz)
        #                          )
        self.node_emb = nn.Sequential(
            #nn.LayerNorm(node_emb_sz,elementwise_affine=False),
            nn.Linear(node_emb_sz,node_emb_sz),
            nn.LeakyReLU()
            )
        self.depth = depth
        self.scale_features = scale_features
        self.scale_steps = (1-scale_features)/self.depth
        self.feat_emb = FeatureEmbedder(self.feat_emb_sz,node_emb_sz,scale=scale_features,n_layers=n_layers)
        self.weight = nn.Sequential(
            nn.Linear(self.node_emb_sz,1,bias=False)
        )
        self.value_head = nn.Sequential(
            nn.Linear(node_emb_sz,1, bias=False)
        )
        self.rezero_param = nn.Parameter(torch.zeros(1))
        #torch.nn.init.normal_(self.weight.weight, 0,0.01)
        #torch.nn.init.constant_(self.weight.bias, 0)
        #self.node_emb.apply(init)
        self.apply(init_ortho)
        self.weight.apply(init)
        #self.feat_emb.apply(init)
        #self.value_head.apply(init)

    def forward(self, raw_feats : torch.Tensor, uids : torch.LongTensor, id_map : torch.LongTensor) -> Tuple[torch.Tensor,torch.Tensor, torch.Tensor]:
        #indices_sorted = torch.argsort(uids, dim=0)
        
        #raws = raw_feats[indices_sorted]
        #id_map = id_map[indices_sorted]
        #sorted_feats = torch.cat([raws, torch.zeros((1,self.feat_emb_sz), device=raw_feats.device)])
        
        sorted_feats = torch.cat([raw_feats, torch.zeros((1,self.feat_emb_sz), device=raw_feats.device)])
        uids = torch.cat([uids, torch.ones(1,dtype=int)*(-1)])
        # now embedd them:
        sorted_feats = self.feat_emb(sorted_feats)
        #print("raw and embedded feats",raw_feats.mean(), sorted_feats.mean())
        
        # this is also used for the fixed input features, but not with the extra "no neighbor" feature
        # inital_feat = sorted_feats[:-1].clone()

        ids2indices = find_neighbor_indices(uids,id_map)
        for _ in range(self.depth):
            # 1 retrieve the relevant features using id_map
            # sorted_feats[id_map].reshape(raw_feats.shape[0],-1)
            # print(sorted_feats[ids2indices].shape)
            # feats_l,feats_r = torch.chunk(sorted_feats[ids2indices].reshape(len(uids)-1,-1),2,-1)
            #feats_l = self.node_emb(feats_l)
            #feats_r = self.node_emb(feats_r)
            feats = sorted_feats[ids2indices].mean(1)#(feats_r + feats_l)/2
            #print("feats", feats.shape)
            feats = self.node_emb(feats)
            # print("feats",feats.shape, id_map.shape, inital_feat.shape)
            new = sorted_feats[:-1] + feats*self.scale_steps*self.rezero_param
            # we ignore the first entry since that is simply the "no neighbor" case
            sorted_feats[:-1] = new
        # undo the sorting and remove the synthetic "no neighbor" node
        x = sorted_feats[:-1]#[uids]
        #print(x.mean(0),x.std(0))
        w = self.weight(x)#torch.tanh(self.weight(x)/10)*10 # this is approximately f(x) = x for small x
        #f = inital_feat[uids]
        v  = self.value_head(x.detach())
        return x, w, v


class NaiveCombineEmbedder(nn.Module):
    def __init__(self, node_emb_sz: int,early_stop:float):
        super().__init__()
        self.node_emb_sz = node_emb_sz
        #self.embd = nn.Sequential(nn.Linear(feature_emb_in+2*node_emb_sz, node_emb_sz),nn.BatchNorm1d(node_emb_sz))
        self.prob = nn.Linear(node_emb_sz, 1)
        self.early_stop = early_stop

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        # linear layer with pseudo-skip connection
        if torch.rand(1)< self.early_stop:
            x = x.detach()
        feat, nodeL, nodeR = torch.chunk(x, 3, -1)
        #x = (feat + nodeL + nodeR)/3
        feat = torch.sigmoid(feat)
        x = feat*nodeL + (1-feat)*nodeR
        p =self.prob(x)
        return x, p