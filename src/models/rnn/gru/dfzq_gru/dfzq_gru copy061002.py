import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_, weight_norm
from typing import Optional, Tuple
from .get_configs import DFZQGRUConfig


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


class PreProj(nn.Module):
    """
    输入投影层：把异质特征投影到统一高维空间
    作用：投影器 + 变量选择器 + 非线性增强器
    """
    def __init__(self, d_in: int, d_h: int, dropout: float = 0.1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_in, d_h),
            nn.ReLU(),
            nn.Dropout(dropout)
        )
        # 简单的Xavier初始化
        self._init_weights()
    
    def _init_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight, gain=nn.init.calculate_gain('relu'))
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: [B, T, d_in] - 原始特征序列
        Returns:
            projected: [B, T, d_h] - 投影后的特征序列
        """
        return self.net(x)


class DFZQGRU(nn.Module):
    """
    简化版 DFZQ GRU：PreProj → GRU → Attention → Head → Predict
    遵循现代时序模型的"浅投影-深时序-浅头"三段式布局
    """
    
    def __init__(self, config: DFZQGRUConfig):
        super().__init__()
        d_in = config.input_size
        d_h = config.hidden_size
        n_layers = config.num_layers
        dropout = config.dropout
        bidirectional = config.bidirectional
        use_attention = config.attention
        
        # 1. 输入投影层：异质特征 → 统一表示
        self.pre_proj = PreProj(d_in, d_h, dropout=dropout)
        
        # 2. GRU 编码器：专注时序建模
        self.gru = nn.GRU(
            input_size=d_h,  # 注意：这里是d_h，因为输入已经被投影了
            hidden_size=d_h,
            num_layers=n_layers,
            batch_first=True,
            dropout=dropout if n_layers > 1 else 0.0,
            bidirectional=bidirectional
        )
        
        # GRU输出维度
        gru_out_dim = d_h * (2 if bidirectional else 1)
        
        # 3. 注意力机制（可选）
        self.use_attention = use_attention
        if use_attention:
            self.attention = AdditiveAttn(
                dim=gru_out_dim,
                dropout=dropout,
                init_tau=getattr(config, 'attn_tau', 2.0),
                learnable_tau=getattr(config, 'learnable_tau', True)
            )
            # 🎯 对context进行LayerNorm，统一last_hidden和context的数值范围
            self.context_norm = nn.LayerNorm(gru_out_dim, elementwise_affine=False)
        
        # 🎯 对 last_hidden 做 LayerNorm，确保与 context 分布对齐
        self.last_hidden_ln = nn.LayerNorm(gru_out_dim, elementwise_affine=False)
        
        # 4. 头部投影层：根据配置选择简单版或MLP版
        fused_dim = gru_out_dim * 2 if use_attention else gru_out_dim
        output_head_type = getattr(config, 'output_head_type', 'simple')  # 'simple' or 'mlp'
        
        if output_head_type == 'mlp':
            # MLP版本：额外加一层小MLP，但最后还是输出到d_h维度
            self.head_proj = nn.Sequential(
                nn.Linear(fused_dim, d_h, bias=False),
                # nn.BatchNorm1d(d_h, affine=False),
                nn.LayerNorm(d_h, elementwise_affine=False), # <--- 替换为LayerNorm
                nn.ReLU(),
                nn.Linear(d_h, d_h, bias=False),  # 小MLP层：d_h → d_h
            )
        else:
            # Simple版本：直接投影
            self.head_proj = nn.Sequential(
                nn.Linear(fused_dim, d_h, bias=False),
            )
        
        # 5. 最终的BatchNorm + Mean输出
        self.bn1 = nn.BatchNorm1d(d_h, affine=False, track_running_stats=True)
        
        # 初始化权重
        self._init_weights()
    
    def _init_weights(self):
        """权重初始化：GRU使用正交初始化，线性层使用 Xavier 初始化"""
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
        
        # 其余线性层初始化
        for module in self.modules():
            if isinstance(module, nn.Linear):
                # 跳过GRU内部的线性层和已经初始化的PreProj
                if any(module is gru_param for gru_param in self.gru.modules() if isinstance(gru_param, nn.Linear)):
                    continue
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
    
    def forward(self, x: torch.Tensor, h0: Optional[torch.Tensor] = None, monitor_gates: bool = False):
        """
        前向传播
        
        Args:
            x: 输入特征 [B, T, D] (已在dataloader中转换好格式)
            h0: 初始隐藏状态（可选）
            monitor_gates: 是否监控GRU门控激活（支持多层单向GRU）
        Returns:
            如果monitor_gates=False:
                pred: 预测结果 [B, 1]
                fv: 特征向量 [B, hidden_size]
            如果monitor_gates=True:
                pred: 预测结果 [B, 1]
                fv: 特征向量 [B, hidden_size]
                gate_stats: dict, 包含每层的gate统计
        """
        
        # 数据格式检查：现在期望输入已经是 [B, T, D] 格式
        if x.dim() == 3:
            B, dim1, dim2 = x.shape
            # 根据维度大小判断：通常特征维度 < 时间维度
            if dim1 < dim2:  # [B, D, T] -> [B, T, D]
                x = x.transpose(1, 2)
        
        # 1. 输入投影：异质特征 → 统一表示
        x = self.pre_proj(x)  # [B, T, d_in] → [B, T, d_h]
        
        # 2. GRU编码：专注时序建模
        if not monitor_gates:
            gru_out, hidden = self.gru(x, h0)  # [B, T, d_h] → [B, T, gru_out_dim]
        else:
            # 门控监控功能（保留原有逻辑）
            assert not self.gru.bidirectional, "monitor_gates仅支持单向GRU"
            B, T, D = x.shape
            num_layers = self.gru.num_layers
            hidden_size = self.gru.hidden_size
            
            if h0 is not None:
                h = h0
            else:
                h = torch.zeros(num_layers, B, hidden_size, device=x.device, dtype=x.dtype)
            
            gate_stats = {}
            for layer in range(num_layers):
                gate_stats[f'layer_{layer}_reset_gate'] = []
                gate_stats[f'layer_{layer}_update_gate'] = []
                gate_stats[f'layer_{layer}_new_gate_pre'] = []
                gate_stats[f'layer_{layer}_new_gate'] = []
            
            current_input = x
            hidden_states = []
            
            for layer in range(num_layers):
                W_ih = getattr(self.gru, f'weight_ih_l{layer}')
                W_hh = getattr(self.gru, f'weight_hh_l{layer}')
                b_ih = getattr(self.gru, f'bias_ih_l{layer}')
                b_hh = getattr(self.gru, f'bias_hh_l{layer}')
                
                h_t = h[layer]
                layer_outputs = []
                
                for t in range(T):
                    x_t = current_input[:, t, :]
                    gates = (torch.matmul(x_t, W_ih.t()) + b_ih) + (torch.matmul(h_t, W_hh.t()) + b_hh)
                    
                    r = torch.sigmoid(gates[:, :hidden_size])
                    z = torch.sigmoid(gates[:, hidden_size:2*hidden_size])
                    n_pre = gates[:, 2*hidden_size:3*hidden_size]
                    n = torch.tanh(n_pre)
                    
                    h_t = (1 - z) * n + z * h_t
                    layer_outputs.append(h_t.unsqueeze(1))
                    
                    gate_stats[f'layer_{layer}_reset_gate'].append(r.detach().cpu())
                    gate_stats[f'layer_{layer}_update_gate'].append(z.detach().cpu())
                    gate_stats[f'layer_{layer}_new_gate_pre'].append(n_pre.detach().cpu())
                    gate_stats[f'layer_{layer}_new_gate'].append(n.detach().cpu())
                
                current_input = torch.cat(layer_outputs, dim=1)
                hidden_states.append(h_t)
            
            gru_out = current_input
            
            for k in gate_stats:
                gate_stats[k] = torch.stack(gate_stats[k], dim=1)
        
        # 3. 聚合表示：last_hidden + 可选的注意力context
        last_hidden = gru_out[:, -1, :]  # [B, gru_out_dim]
        last_hidden_normed = self.last_hidden_ln(last_hidden)
        
        if self.use_attention:
            context, attn_weights = self.attention(gru_out)  # [B, gru_out_dim]
            context = self.context_norm(context)
            combined = torch.cat([last_hidden_normed, context], dim=1)  # [B, 2*gru_out_dim]
        else:
            combined = last_hidden_normed  # [B, gru_out_dim]
        
        # 4. 头部投影：融合表示 → 预测表示
        fv = self.head_proj(combined)  # [B, d_h]
        
        # 5. BatchNorm + Mean输出：锁定特征尺度，然后取均值作为预测
        fv = self.bn1(fv)                      # [B, d_h] - 控制特征分布
        pred = fv.mean(dim=1, keepdim=True)    # [B, 1] - 取均值作为最终预测
        
        if monitor_gates:
            return pred, fv, gate_stats
        else:
            return pred, fv
    
    def clip_gradients(self, max_norm: float = 5.0):
        """全局梯度裁剪，在训练循环中调用"""
        return clip_grad_norm_(self.parameters(), max_norm)
