from typing import Union, Dict, Any

import torch
from torch import Tensor
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import Set2Set
from torch_geometric.data import Data, Batch
from torch_geometric.utils import scatter

from crystal_gnn.models.base_module import BaseModule
from crystal_gnn.models.module_utils import RBFExpansion, ShiftedSoftplus
from crystal_gnn.layers.mlp_readout import MLPReadout


class MEGNET(BaseModule):
    """MEGNET Model.

    "Graph Networks as a Universal Machine Learning Framework for Molecules
    and Crystals"
    Chem. Mater. (2019)
    https://pubs.acs.org/doi/10.1021/acs.chemmater.9b01294
    """

    def __init__(self, _config: Dict[str, Any]) -> None:
        super().__init__(_config)
        # config
        self.num_conv = _config["num_conv"]

        self.hidden_dim = _config["hidden_dim"]
        self.rbf_distance_dim = _config["rbf_distance_dim"]
        self.batch_norm = _config["batch_norm"]
        self.dropout = _config["dropout"]
        self.residual = _config["residual"]
        self.cutoff = _config["cutoff"]
        self.global_dim = 2

        # layers
        self.nonlinear = ShiftedSoftplus()
        self.node_embedding = nn.Sequential(
            nn.Embedding(103, self.hidden_dim * 2),
            self.nonlinear,
            nn.Linear(self.hidden_dim * 2, self.hidden_dim, bias=True),
            self.nonlinear,
        )
        self.rbf_expansion = RBFExpansion(
            vmin=0, vmax=self.cutoff, bins=self.rbf_distance_dim
        )
        self.edge_embedding = nn.Sequential(
            nn.Linear(self.rbf_distance_dim, self.hidden_dim * 2, bias=True),
            self.nonlinear,
            nn.Linear(self.hidden_dim * 2, self.hidden_dim, bias=True),
            self.nonlinear,
        )
        self.global_embedding = nn.Sequential(
            nn.Linear(self.global_dim, self.hidden_dim * 2, bias=True),
            self.nonlinear,
            nn.Linear(self.hidden_dim * 2, self.hidden_dim, bias=True),
            self.nonlinear,
        )
        self.megnet_blocks = nn.ModuleList(
            [
                MEBNETBlock(
                    hidden_dim=self.hidden_dim,
                    batch_norm=self.batch_norm,
                    residual=self.residual,
                    dropout=self.dropout,
                )
                for _ in range(self.num_conv)
            ]
        )

        self.set2set_node = Set2Set(self.hidden_dim, processing_steps=3)
        self.set2set_edge = Set2Set(self.hidden_dim, processing_steps=3)
        self.readout = MLPReadout(
            self.hidden_dim * 5,
            self.readout_dim,
            bias=True,
            nonlinear="shifted_softplus",
        )

        self.apply(self._init_weights)

    def forward(self, data: Union[Data, Batch]) -> torch.Tensor:
        # node embedding
        node_attrs = data.x  # [B_n]
        node_feats = self.node_embedding(node_attrs)  # [B_n, H]
        # edge embedding
        distances = torch.norm(data.relative_vec, dim=-1)  # [B_e]
        edge_feats = self.rbf_expansion(distances)  # [B_e, D]
        edge_feats = self.edge_embedding(edge_feats)  # [B_e, H]
        # global embedding
        if hasattr(data, "batch_size"):
            global_attrs = torch.zeros(data.batch_size, self.global_dim).to(self.device)
            batch = data.batch
        else:
            global_attrs = torch.zeros(1, self.global_dim)
            batch = torch.zeros(data.num_nodes, dtype=torch.long).to(self.device)
        global_feats = self.global_embedding(global_attrs)  # [B, H]
        # conv layers
        for megnet_block in self.megnet_blocks:
            node_feats, edge_feats, global_feats = megnet_block(
                node_feats,
                edge_feats,
                global_feats,
                data.edge_index,
                batch=batch,
            )  # [B_n, H], [B_e, H], [B, H]
        # pooling
        idx_src, _ = data.edge_index
        node_feats = self.set2set_node(node_feats, batch)  # [B, 2H]
        edge_feats = self.set2set_edge(edge_feats, batch[idx_src])  # [B, 2H]
        # concat
        out = torch.cat([node_feats, edge_feats, global_feats], dim=1)  # [B, 5H]
        # readout
        out = self.readout(out)  # [B, H]
        return out


class MEBNETBlock(nn.Module):
    def __init__(
        self,
        hidden_dim: int,
        batch_norm: bool,
        residual: bool,
        dropout: float,
    ):
        super().__init__()
        self.batch_norm = batch_norm
        self.residual = residual
        self.dropout = dropout

        self.nonlinear = ShiftedSoftplus()

        self.lin_node_1 = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2, bias=True),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=True),
            self.nonlinear,
        )
        self.lin_edge_1 = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2, bias=True),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=True),
            self.nonlinear,
        )
        self.lin_global_1 = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2, bias=True),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=True),
            self.nonlinear,
        )

        self.fn_edge_update = EdgeModel(
            n_node_features=hidden_dim,
            n_edge_features=hidden_dim,
            n_global_features=hidden_dim,
            hidden_dim=hidden_dim,
        )
        self.fn_node_update = NodeModel(
            n_node_features=hidden_dim,
            n_edge_features=hidden_dim,
            n_global_features=hidden_dim,
            hidden_dim=hidden_dim,
        )
        self.fn_global_model = GlobalModel(
            n_node_features=hidden_dim,
            n_edge_features=hidden_dim,
            n_global_features=hidden_dim,
            hidden_dim=hidden_dim,
        )

        self.bn_node = nn.BatchNorm1d(hidden_dim)
        self.bn_edge = nn.BatchNorm1d(hidden_dim)
        self.bn_global = nn.BatchNorm1d(hidden_dim)

    def forward(
        self,
        node_feats: Tensor,
        edge_feats: Tensor,
        global_feats: Tensor,
        edge_index: Tensor,
        batch: Tensor,
    ) -> Tensor:
        # clone for residual connection
        orig_node_feats = node_feats.clone()  # [B_n, H]
        orig_edge_feats = edge_feats.clone()  # [B_e, H]
        orig_global_feats = global_feats.clone()  # [B, H]
        # linear
        node_feats = self.nonlinear(self.lin_node_1(node_feats))  # [B_n, H_]
        edge_feats = self.nonlinear(self.lin_edge_1(edge_feats))  # [B_e, H_]
        global_feats = self.nonlinear(self.lin_global_1(global_feats))  # [B, H_]
        # meta layer
        edge_feats = self.fn_edge_update(
            node_feats, edge_feats, global_feats, edge_index, batch
        )  # [B_e, H_]
        node_feats = self.fn_node_update(
            node_feats, edge_feats, global_feats, edge_index, batch
        )  # [B_n, H_]
        global_feats = self.fn_global_model(
            node_feats, edge_feats, global_feats, edge_index, batch
        )  # [B, H_]
        # batch norm
        if self.batch_norm:
            node_feats = self.bn_node(node_feats)  # [B_n, H]
            edge_feats = self.bn_edge(edge_feats)  # [B_e, H]
            global_feats = self.bn_global(global_feats)  # [B, H]
        # residual connection
        if self.residual:
            node_feats += orig_node_feats  # [B_n, H]
            edge_feats += orig_edge_feats  # [B_e, H]
            global_feats += orig_global_feats  # [B, H]
        # dropout
        node_feats = F.dropout(node_feats, p=self.dropout, training=self.training)
        edge_feats = F.dropout(edge_feats, p=self.dropout, training=self.training)
        global_feats = F.dropout(global_feats, p=self.dropout, training=self.training)
        return node_feats, edge_feats, global_feats


class EdgeModel(nn.Module):
    def __init__(
        self,
        n_node_features: int,
        n_edge_features: int,
        n_global_features: int,
        hidden_dim: int,
        lin_bias: bool = True,
    ) -> None:
        super().__init__()
        self.nonlinear = ShiftedSoftplus()
        self.mlp = nn.Sequential(
            nn.Linear(
                n_node_features * 2 + n_edge_features + n_global_features,
                hidden_dim * 2,
                bias=lin_bias,
            ),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim * 2, bias=lin_bias),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=lin_bias),
            self.nonlinear,
        )

    def forward(
        self,
        node_feats: Tensor,
        edge_feats: Tensor,
        global_feats: Tensor,
        edge_index: Tensor,
        batch: Tensor,
    ) -> Tensor:
        idx_src, idx_dst = edge_index  # [B_e], [B_e]
        batch_edge_map = batch[idx_src]  # [B_e]
        out = torch.cat(
            [
                node_feats[idx_src],
                node_feats[idx_dst],
                edge_feats,
                global_feats[batch_edge_map],
            ],
            dim=1,
        )  # [B_e, 4H_]
        return self.mlp(out)  # [B_e, H_]


class NodeModel(nn.Module):
    def __init__(
        self,
        n_node_features: int,
        n_edge_features: int,
        n_global_features: int,
        hidden_dim: int,
        lin_bias: bool = True,
    ) -> None:
        super().__init__()
        self.nonlinear = ShiftedSoftplus()
        self.mlp = nn.Sequential(
            nn.Linear(
                n_node_features + n_edge_features + n_global_features,
                hidden_dim * 2,
                bias=lin_bias,
            ),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim * 2, bias=lin_bias),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=lin_bias),
            self.nonlinear,
        )

    def forward(
        self,
        node_feats: Tensor,
        edge_feats: Tensor,
        global_feats: Tensor,
        edge_index: Tensor,
        batch: Tensor,
    ) -> Tensor:
        _, idx_dst = edge_index
        edge_feats = scatter(edge_feats, idx_dst, dim=0, reduce="mean")  # [B_n, H_]
        out = torch.cat(
            [node_feats, edge_feats, global_feats[batch]], dim=1
        )  # [B_n, 3H_]
        return self.mlp(out)  # [B_n, H_]


class GlobalModel(nn.Module):
    def __init__(
        self,
        n_node_features: int,
        n_edge_features: int,
        n_global_features: int,
        hidden_dim: int,
        lin_bias: bool = True,
    ) -> None:
        super().__init__()
        self.nonlinear = ShiftedSoftplus()
        self.mlp = nn.Sequential(
            nn.Linear(
                n_node_features + n_edge_features + n_global_features,
                hidden_dim * 2,
                bias=lin_bias,
            ),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim * 2, bias=lin_bias),
            self.nonlinear,
            nn.Linear(hidden_dim * 2, hidden_dim, bias=lin_bias),
            self.nonlinear,
        )

    def forward(
        self,
        node_feats: Tensor,
        edge_feats: Tensor,
        global_feats: Tensor,
        edge_index: Tensor,
        batch: Tensor,
    ) -> Tensor:
        idx_src, _ = edge_index
        edge_feats = scatter(
            edge_feats, batch[idx_src], dim=0, reduce="mean"
        )  # [B, H_]
        node_feats = scatter(node_feats, batch, dim=0, reduce="mean")  # [B, H_]
        out = torch.cat([node_feats, edge_feats, global_feats], dim=1)  # [B, 3H_]
        return self.mlp(out)  # [B, H_]
