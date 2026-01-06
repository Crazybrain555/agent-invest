import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_, weight_norm
from typing import Optional, Tuple
from .get_configs import DFZQGRUConfig


class ResidualMLPBlock(nn.Module):
    """
    归一化类型可选的 MLP-ResNet 块，使用InstanceNorm1d替代BatchNorm
    norm_type: "instance" | "layer" | "group" | None
    """
    
    def __init__(
        self,
        dim: int,
        hidden_dim: Optional[int] = None,
        norm_type: str = "layer",  # "instance" | "layer" | "group" | None
        num_groups: int = 1,       # GroupNorm 用
        dropout: float = 0.1,
        activation: str = "relu",
        scale: float = 1.0
    ):
        super().__init__()
        hidden_dim = hidden_dim or dim
        
        # 选择归一化类型
        if norm_type == "instance":
            # InstanceNorm1d 用于每个样本独立归一化，避免时序泄漏
            self.norm = nn.InstanceNorm1d(dim, affine=True, track_running_stats=False)
            self._is_instance = True
        elif norm_type == "group":
            self.norm = nn.GroupNorm(num_groups=num_groups, num_channels=dim, affine=True)
            self._is_instance = False
        elif norm_type == "layer":
            self.norm = nn.LayerNorm(dim, elementwise_affine=True)
            self._is_instance = False
        else:  # None
            self.norm = None
            self._is_instance = False
            
        self.fc1 = nn.Linear(dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, dim)
        self.dropout = nn.Dropout(dropout)
        self.scale = scale
        
        # 选择激活函数
        if activation.lower() == "gelu":
            self.activation = nn.GELU()
        elif activation.lower() == "relu":
            self.activation = nn.ReLU()
        else:
            self.activation = nn.ReLU()  # 默认使用ReLU

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 归一化处理
        if self.norm is not None:
            # ============ 可选patch：分离处理特征和mask ============
            # 如果输入是[特征+mask]格式，只对特征部分进行归一化
            # D = x.size(-1) // 2
            # x_main, x_mask = x[..., :D], x[..., D:]
            # 
            # if self._is_instance:  # InstanceNorm 需要 transpose
            #     x_main = self.norm(x_main.transpose(1, 2)).transpose(1, 2)
            # else:
            #     x_main = self.norm(x_main)
            # 
            # x = torch.cat([x_main, x_mask], dim=-1)
            # ========================================================
            
            # 当前默认做法：直接对整个输入进行归一化
            if self._is_instance:  # InstanceNorm 需要 transpose
                x_normed = self.norm(x.transpose(1, 2)).transpose(1, 2)
            else:
                x_normed = self.norm(x)
        else:
            x_normed = x
        out = self.fc1(x_normed)
        out = self.activation(out)
        out = self.dropout(out)
        out = self.fc2(out)
        # 最后相加残差
        return x + self.scale * out


class AdditiveAttn(nn.Module):
    """
    加性注意力机制：LayerNorm → Linear → Tanh → Dropout → Linear → "去中心化 + RMS归一化" → Softmax
    相比原始Sequential写法，提供更好的可维护性和初始化稳定性
    + 使用 Weight-Norm 解耦 v 的方向和幅度
    + 使用 RMS 约束替代传统的 τ 缩放，避免 Softmax 饱和
    """
    def __init__(self, dim, dropout=0.1, init_tau=2.0, learnable_tau=True):
        super().__init__()
        self.ln = nn.LayerNorm(dim)                    # ① LayerNorm
        self.fc = nn.Linear(dim, dim // 2)             # ② Linear → tanh → dropout
        self.act = nn.Tanh()
        self.drop = nn.Dropout(dropout)
        
        # ③ 🎯 使用 weight_norm 解耦 v 的幅度和方向，避免梯度消失
        self.v = weight_norm(nn.Linear(dim // 2, 1, bias=False), name="weight", dim=None)
        
        # 温度参数：保留用于向后兼容，但主要使用RMS归一化
        if learnable_tau:
            self.tau = nn.Parameter(torch.tensor(init_tau))
        else:
            self.register_buffer("tau", torch.tensor(init_tau))
        
        # 初始化：让fc的权重小一点，避免tanh饱和
        nn.init.xavier_uniform_(self.fc.weight, gain=nn.init.calculate_gain('tanh')/2)
        ## weight_norm的v已经有自己的初始化，无需额外处理
        # nn.init.xavier_uniform_(self.v.weight, gain=1.0)
        if self.fc.bias is not None:
            nn.init.zeros_(self.fc.bias)
    
    def forward(self, h):
        """
        Args:
            h: [B, T, D] - GRU输出序列
        Returns:
            context: [B, D] - 加权上下文向量
            weights: [B, T, 1] - 注意力权重
        """
        # 1) LayerNorm
        z = self.ln(h)                           # [B, T, D]
        # 2) Linear → tanh → Dropout
        z = self.act(self.fc(z))                 # [B, T, D//2]
        z = self.drop(z)
        # 3) 计算原始打分
        raw_scores = self.v(z)                   # [B, T, 1]
        
        # 4-5) 🎯 去中心化 + RMS 归一化（以标准差代替1/τ，保证softmax在线性区）
        centered = raw_scores - raw_scores.mean(dim=1, keepdim=True)  # [B, T, 1]
        
        rms = centered.std(dim=1, keepdim=True) + 1e-5                # 每个序列的 σ + ε
        scaled = centered / rms                                       # [B, T, 1] - 标准差≈1
        
        # 6) Softmax 归一化（现在始终在线性区，梯度p(1-p)不会消失）
        weights = torch.softmax(scaled, dim=1)   # [B, T, 1]
        
        # 7) 加权求和得到 context
        context = (h * weights).sum(dim=1)       # [B, D]
        return context, weights


class DFZQGRU(nn.Module):
    """简化版 DFZQ GRU：ResidualMLP → GRU → Attention → Head → Predict"""
    
    def __init__(self, config: DFZQGRUConfig):
        super().__init__()
        d_in = config.input_size
        d_h = config.hidden_size
        n_layers = config.num_layers
        dropout = config.dropout
        bidirectional = config.bidirectional
        use_attention = config.attention
        
        # 归一化配置
        input_norm_type = getattr(config, 'input_norm_type', None)  # "instance" | "layer" | "group" | None
        feature_norm_type = getattr(config, 'feature_norm_type', 'layer')
        use_pre_gru_norm = getattr(config, 'use_pre_gru_norm', False)
        
        # 1. 输入预处理 - 残差MLP块
        input_hidden = getattr(config, 'input_hidden_dim', None) or d_h
        self.input_res = ResidualMLPBlock(
            dim=d_in, 
            hidden_dim=input_hidden,
            norm_type=input_norm_type,  # 可选：首层用 "layer" 避免时序泄漏
            dropout=dropout,
            activation="relu", 
            scale=0.5
            # scale=1
        )
        
        # 2. 可选的 pre-GRU 归一化（轻量级 LayerNorm）
        self.use_pre_gru_norm = use_pre_gru_norm
        if use_pre_gru_norm:
            self.pre_gru_norm = nn.BatchNorm1d(d_in, elementwise_affine=True)
        
        # 3. GRU 编码器
        self.gru = nn.GRU(
            input_size=d_in,  # 注意这里改回d_in，因为input_res输出仍是d_in维度
            hidden_size=d_h,
            num_layers=n_layers,
            batch_first=True,
            dropout=dropout if n_layers > 1 else 0.0,
            bidirectional=bidirectional
        )
        
        # GRU输出维度
        gru_out_dim = d_h * (2 if bidirectional else 1)
        
        # 4. 注意力机制（可选）
        self.use_attention = use_attention
        if use_attention:
            # 用AdditiveAttn代替原来的Sequential + 手动tau
            self.attention = AdditiveAttn(
                dim=gru_out_dim,
                dropout=dropout * 1,  # 保持与原来一致的dropout率
                init_tau=getattr(config, 'attn_tau', 2.0),
                learnable_tau=getattr(config, 'learnable_tau', True)  # 从配置文件读取
            )
            # 🎯 新增：对context进行LayerNorm，统一last_hidden和context的数值范围
            self.context_norm = nn.LayerNorm(gru_out_dim, elementwise_affine=False)
        
        # 🎯 新增：对 last_hidden 做 BatchNorm，确保与 context 分布对齐
        self.last_hidden_ln = nn.LayerNorm(gru_out_dim, elementwise_affine=False)
        
        # 5. 头部投影层：Linear → BatchNorm1d → ReLU
        fused_dim = gru_out_dim * 2 if use_attention else gru_out_dim
        self.head_proj = nn.Sequential(
            nn.Linear(fused_dim, d_h, bias=False),           # bias=False，因为紧跟 BN
            nn.BatchNorm1d(d_h, affine=True),                 # 保留 affine，让网络学 γ/β
            # nn.ReLU(),
        )
        
        #bn_out
        self.bn_out = nn.BatchNorm1d(1, affine=False)
        
        # self.head_proj_post_ln = nn.BatchNorm1d(d_h, affine=False)
        
        # 6. 额外的特征增强层（可选）
        # head_hidden = getattr(config, 'head_hidden_dim', None) or d_h * 2
        # self.feature_mlp = ResidualMLPBlock(
        #     dim=d_h,
        #     hidden_dim=head_hidden,
        #     norm_type=feature_norm_type,  # 后续层保持 LayerNorm
        #     dropout=dropout,
        #     # activation="relu",
        #     activation="gelu",
        #     scale=0.5
        #     # scale=1
        # )
        
        #真·PreAct：add 之后再 LN
        # self.feature_mlp_post_ln = nn.LayerNorm(d_h, elementwise_affine=False)
        
        # # 5-6. 简化版：直接用三层MLP从combined到pred
        # self.simple_mlp = nn.Sequential(
        #     nn.Linear(d_h, d_h, bias=False),               # 紧跟 BN
        #     nn.BatchNorm1d(d_h, affine=True),
        #     nn.GELU(),
            
        #     nn.Dropout(dropout),                            #再加一个
            
        #     nn.Linear(d_h, d_h // 2, bias=False),           # 紧跟 BN
        #     nn.BatchNorm1d(d_h // 2, affine=True),
        #     # nn.GELU(),
        #     # nn.Dropout(dropout),
        #     # nn.Linear(d_h // 2, config.output_size)
            
        # )
        
        # 初始化权重
        self._init_weights()  # 
    
    def _init_weights(self):
        """权重初始化：GRU使用正交初始化，线性层使用 Xavier/KaB等初始化"""
        # GRU特殊初始化
        for name, param in self.gru.named_parameters():
            if 'weight_ih' in name:
                nn.init.xavier_uniform_(param, gain=1.0)
            elif 'weight_hh' in name:
                nn.init.orthogonal_(param, gain=1.0)
            elif 'bias' in name:
                nn.init.zeros_(param)
                hidden_size = param.size(0) // 3
                # 重置门偏置设为 -0.2，更新门偏置设为 +0.2
                param.data[0:hidden_size].fill_(-0.2)
                param.data[hidden_size:2*hidden_size].fill_(0.2)

        # 其余 Linear/LayerNorm/BatchNorm 的初始化
        # 对 Sequential 结构做 lookahead，ReLU 前用 kaiming，其余用 xavier
        def init_sequential_with_relu(seq):
            modules = list(seq)
            for i, module in enumerate(modules):
                if isinstance(module, nn.Linear):
                    next_is_relu = (i + 1 < len(modules)) and isinstance(modules[i + 1], nn.ReLU)
                    if next_is_relu:
                        nn.init.kaiming_normal_(module.weight, nonlinearity='relu')
                    else:
                        nn.init.xavier_uniform_(module.weight, gain=1.0)
                    if module.bias is not None:
                        nn.init.zeros_(module.bias)
        # 对 head_proj
        if isinstance(getattr(self, 'head_proj', None), nn.Sequential):
            init_sequential_with_relu(self.head_proj)
        # # 对 simple_mlp
        # if isinstance(getattr(self, 'simple_mlp', None), nn.Sequential):
        #     init_sequential_with_relu(self.simple_mlp)
        # 其他非 Sequential Linear
        for module in self.modules():
            if isinstance(module, nn.Linear):
                # 跳过已在 Sequential 中初始化的 Linear
                in_head_proj = any(module is m for m in getattr(self.head_proj, 'children', lambda:[])())
                # in_simple_mlp = any(module is m for m in getattr(self.simple_mlp, 'children', lambda:[])())
                # if in_head_proj or in_simple_mlp:
                if in_head_proj:
                    continue
                if module not in self._get_gru_linear_layers():
                    nn.init.xavier_uniform_(module.weight, gain=1.0)
                    if module.bias is not None:
                        nn.init.zeros_(module.bias)
            elif isinstance(module, nn.LayerNorm):
                if module.elementwise_affine:
                    nn.init.ones_(module.weight)
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.BatchNorm1d):
                if hasattr(module, 'weight') and module.weight is not None:
                    nn.init.ones_(module.weight)
                if hasattr(module, 'bias') and module.bias is not None:
                    nn.init.zeros_(module.bias)
        # AdditiveAttn 已有各自初始化逻辑，无需额外重复
    
    def _get_gru_linear_layers(self):
        """获取GRU内部的Linear层，避免重复初始化"""
        gru_layers = []
        for module in self.gru.modules():
            if isinstance(module, nn.Linear):
                gru_layers.append(module)
        return gru_layers
    
    def forward(self, x: torch.Tensor, h0: Optional[torch.Tensor] = None, monitor_gates: bool = False):
        """
        前向传播
        
        Args:
            x: 输入特征 [B, T, D] (已在dataloader中转换好格式)
            h0: 初始隐藏状态（可选）
            monitor_gates: 是否监控GRU门控激活（支持多层单向GRU）
        Returns:
            如果monitor_gates=False:
                pred: 预测结果 [B, output_size]
                fv: 特征向量 [B, hidden_size]
            如果monitor_gates=True:
                pred: 预测结果 [B, output_size]
                fv: 特征向量 [B, hidden_size]
                gate_stats: dict, 包含每层的gate统计(layer_X_reset_gate, layer_X_update_gate等)
        """
        
        # 数据格式检查：现在期望输入已经是 [B, T, D] 格式
        # 注释掉原有的转换逻辑，因为dataloader已经处理了
        if x.dim() == 3:
            B, dim1, dim2 = x.shape
            # 根据维度大小判断：通常特征维度 < 时间维度
            if dim1 < dim2:  # [B, D, T] -> [B, T, D]
                x = x.transpose(1, 2)
        
        # x = x.transpose(1, 2)  # 目前没有处理nan，数据格式还是[B, T, D]
        
        # 1. 输入预处理
        x = self.input_res(x)  # [B, T, D]
        
        # 2. 可选的 pre-GRU 归一化
        if self.use_pre_gru_norm:
            # 需要reshape为[B*T, D]进行BatchNorm1d
            B, T, D = x.shape
            x = self.pre_gru_norm(x.reshape(B*T, D)).reshape(B, T, D)
        
        # 3. GRU编码
        if not monitor_gates:
            gru_out, hidden = self.gru(x, h0)
        else:
            # 支持多层单向GRU的门控监控
            assert not self.gru.bidirectional, "monitor_gates仅支持单向GRU"
            B, T, D = x.shape
            num_layers = self.gru.num_layers
            hidden_size = self.gru.hidden_size
            
            # 初始化隐藏状态
            if h0 is not None:
                h = h0
            else:
                h = torch.zeros(num_layers, B, hidden_size, device=x.device, dtype=x.dtype)
            
            # 初始化门控统计 - 为每层创建统计
            gate_stats = {}
            for layer in range(num_layers):
                gate_stats[f'layer_{layer}_reset_gate'] = []
                gate_stats[f'layer_{layer}_update_gate'] = []
                gate_stats[f'layer_{layer}_new_gate_pre'] = []
                gate_stats[f'layer_{layer}_new_gate'] = []
            
            # 逐层前向传播
            current_input = x  # [B, T, D]
            hidden_states = []
            
            for layer in range(num_layers):
                # 获取当前层的权重
                W_ih = getattr(self.gru, f'weight_ih_l{layer}')
                W_hh = getattr(self.gru, f'weight_hh_l{layer}')
                b_ih = getattr(self.gru, f'bias_ih_l{layer}')
                b_hh = getattr(self.gru, f'bias_hh_l{layer}')
                
                h_t = h[layer]  # 当前层的初始隐藏状态
                layer_outputs = []
                
                # 逐时间步计算
                for t in range(T):
                    x_t = current_input[:, t, :]  # [B, input_size]
                    
                    # 计算门控
                    gates = (torch.matmul(x_t, W_ih.t()) + b_ih) + (torch.matmul(h_t, W_hh.t()) + b_hh)
                    
                    # 分离三个门
                    r = torch.sigmoid(gates[:, :hidden_size])  # reset gate
                    z = torch.sigmoid(gates[:, hidden_size:2*hidden_size])  # update gate
                    n_pre = gates[:, 2*hidden_size:3*hidden_size]  # new gate (pre-activation)
                    n = torch.tanh(n_pre)  # new gate (post-activation)
                    
                    # 更新隐藏状态
                    h_t = (1 - z) * n + z * h_t
                    layer_outputs.append(h_t.unsqueeze(1))
                    
                    # 保存门控统计
                    gate_stats[f'layer_{layer}_reset_gate'].append(r.detach().cpu())
                    gate_stats[f'layer_{layer}_update_gate'].append(z.detach().cpu())
                    gate_stats[f'layer_{layer}_new_gate_pre'].append(n_pre.detach().cpu())
                    gate_stats[f'layer_{layer}_new_gate'].append(n.detach().cpu())
                
                # 当前层的输出作为下一层的输入
                current_input = torch.cat(layer_outputs, dim=1)  # [B, T, hidden_size]
                hidden_states.append(h_t)
            
            gru_out = current_input  # 最后一层的输出
            
            # 转换门控统计格式
            for k in gate_stats:
                gate_stats[k] = torch.stack(gate_stats[k], dim=1)  # (B, T, H)
                
        last_hidden = gru_out[:, -1, :]
        
        # 🎯 新增：对 last_hidden 做 BatchNorm，确保与 context 分布对齐
        last_hidden_normed = self.last_hidden_ln(last_hidden)  # [B, gru_out_dim]
        
        if self.use_attention:
            # 使用注意力机制获取加权表示
            context, attn_weights = self.attention(gru_out)  # [B, gru_out_dim]
            
            # 🎯 新增：对context进行LayerNorm，统一last_hidden和context的数值范围
            context = self.context_norm(context)
            
            # 连接 BatchNorm后的last_hidden 和 LayerNorm后的context
            combined = torch.cat([last_hidden_normed, context], dim=1)  # [B, 2*gru_out_dim]
        else:
            # 只使用最后时刻的隐藏状态（已经做了BatchNorm）
            combined = last_hidden_normed  # [B, gru_out_dim]
        
        # 5. 头部投影（Linear → BN → ReLU），再做一次 LayerNorm
        fv = self.head_proj(combined)       # [B, d_h]
        
        
        pred = fv.mean(dim=1, keepdim=True)    # [B,1]
        # pred = self.bn_out(pred)               # bn_out = nn.BatchNorm1d(1, affine=False)
        
        
        
        
        # # 6. 预测 MLP（Linear → BN → GELU → Linear → BN → GELU → Dropout → Linear）
        # pred = self.simple_mlp(fv)          # [B, output_size]
        
        if monitor_gates:
            return pred, fv, gate_stats  # 用combined作为特征向量
        else:
            return pred, fv  # 用combined作为特征向量
    
    def clip_gradients(self, max_norm: float = 5.0):
        """全局梯度裁剪，在训练循环中调用"""
        return clip_grad_norm_(self.parameters(), max_norm)
