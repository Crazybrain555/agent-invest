"""
PyTorch nn.Transformer Based Encoder-Only Model

This module implements an encoder-only transformer model using PyTorch's built-in 
torch.nn.Transformer module instead of custom implementation. It maintains the same
input/output structure as the existing models for compatibility.
"""

import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple


class PositionalEncoding(nn.Module):
    """
    Positional encoding for transformer models.
    Uses sinusoidal encoding as described in "Attention is All You Need".
    """
    
    def __init__(self, d_model: int, max_len: int = 5000):
        super().__init__()
        
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        
        self.register_buffer('pe', pe)
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Add positional encoding to input tensor.
        
        Args:
            x: Input tensor of shape (seq_len, batch_size, d_model)
            
        Returns:
            Tensor with positional encoding added
        """
        pe = self.pe[:x.size(0), :]  # type: ignore
        return x + pe


class TorchTransformerEncoder(nn.Module):
    """
    Encoder-only transformer model using torch.nn.Transformer.
    
    This model takes a sequence of features and outputs a single prediction value,
    making it suitable for regression tasks in time series forecasting.
    """
    
    def __init__(self, input_size: int, seq_length: int, d_model: int, nhead: int, 
                 num_encoder_layers: int, dim_feedforward: int, output_size: int = 1,
                 dropout: float = 0.1, activation: str = "gelu", 
                 positional_encoding: bool = True, norm_type: str = "layer",
                 embedding_type: str = "linear", norm_first: bool = True):
        super().__init__()
        
        self.input_size = input_size
        self.seq_length = seq_length
        self.d_model = d_model
        self.output_size = output_size
        self.nhead = nhead
        self.num_encoder_layers = num_encoder_layers
        
        # Input embedding layer
        if embedding_type == "linear":
            self.input_embedding = nn.Linear(input_size, d_model)
        elif embedding_type == "conv":
            self.input_embedding = nn.Conv1d(input_size, d_model, kernel_size=1)
        else:
            raise ValueError(f"Unsupported embedding type: {embedding_type}")
        
        self.embedding_type = embedding_type
        
        # Positional encoding
        if positional_encoding:
            self.pos_encoding = PositionalEncoding(d_model, seq_length)
        else:
            self.pos_encoding = None
        
        # Create encoder layer
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=dim_feedforward,
            dropout=dropout,
            activation=activation,
            layer_norm_eps=1e-5,
            batch_first=False,  # torch.nn.Transformer expects (seq, batch, feature) format
            norm_first=bool(norm_first)
        )
        
        # Create transformer encoder
        if norm_type == "layer":
            encoder_norm = nn.LayerNorm(d_model)
        elif norm_type == "batch":
            encoder_norm = nn.BatchNorm1d(d_model)
        else:
            encoder_norm = None
        
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer, 
            num_layers=num_encoder_layers,
            norm=encoder_norm if norm_type == "layer" else None
        )
        
        # Global pooling (can be mean, max, or last)
        self.pooling = "mean"  # Options: "mean", "max", "last"
        
        # Output projection to feature vector (same as GRU's d_h dimension)
        feature_dim = d_model // 2  # Use d_model//2 as the feature dimension
        self.output_projection = nn.Sequential(
            nn.Linear(d_model, feature_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(feature_dim, feature_dim)
        )
        
        # Final BatchNorm + Mean output (same as DFZQ GRU)
        self.bn1 = nn.BatchNorm1d(feature_dim, affine=False, track_running_stats=True)

        # Initialize weights
        self._init_weights()
    
    def _init_weights(self):
        """Initialize model weights."""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.LayerNorm):
                nn.init.ones_(module.weight)
                nn.init.zeros_(module.bias)
            elif isinstance(module, nn.BatchNorm1d):
                # Only initialize if weight and bias exist (affine=True)
                if hasattr(module, 'weight') and module.weight is not None:
                    nn.init.ones_(module.weight)
                if hasattr(module, 'bias') and module.bias is not None:
                    nn.init.zeros_(module.bias)
    
    def forward(self, x: torch.Tensor, src_mask: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass of the encoder-only transformer.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            src_mask: Optional source mask for transformer
            
        Returns:
            pred: Prediction tensor of shape (batch_size, 1)
            fv: Feature vector of shape (batch_size, feature_dim)
        """
        batch_size, seq_len, input_dim = x.shape
        
        # Input embedding
        if self.embedding_type == "linear":
            # (batch_size, seq_length, input_size) -> (batch_size, seq_length, d_model)
            x = self.input_embedding(x)
            # Transpose for transformer: (seq_length, batch_size, d_model)
            x = x.transpose(0, 1)
        else:  # conv
            # (batch_size, seq_length, input_size) -> (batch_size, input_size, seq_length)
            x = x.transpose(1, 2)
            # (batch_size, d_model, seq_length)
            x = self.input_embedding(x)
            # (batch_size, seq_length, d_model)
            x = x.transpose(1, 2)
            # (seq_length, batch_size, d_model)
            x = x.transpose(0, 1)
        
        # Add positional encoding
        if self.pos_encoding is not None:
            x = self.pos_encoding(x)
        
        # Pass through transformer encoder
        x = self.transformer_encoder(x, mask=src_mask)
        
        # Global pooling to get sequence representation
        if self.pooling == "mean":
            # (seq_length, batch_size, d_model) -> (batch_size, d_model)
            x = torch.mean(x, dim=0)
        elif self.pooling == "max":
            # (seq_length, batch_size, d_model) -> (batch_size, d_model)
            x, _ = torch.max(x, dim=0)
        elif self.pooling == "last":
            # (seq_length, batch_size, d_model) -> (batch_size, d_model)
            x = x[-1]
        
        # Output projection to feature vector
        fv = self.output_projection(x)  # [B, feature_dim]
        
        # BatchNorm + Mean output (same as DFZQ GRU)
        fv = self.bn1(fv)                        # [B, feature_dim] - 控制特征分布
        pred = fv.mean(dim=1, keepdim=True)      # [B, 1] - 取均值作为最终预测
        
        return pred, fv
    
    def get_attention_weights(self, x: torch.Tensor) -> list:
        """
        Get attention weights from all encoder layers.
        
        Note: PyTorch's nn.Transformer doesn't provide direct access to attention weights.
        This method provides a placeholder for compatibility with the existing interface.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            
        Returns:
            Empty list (attention weights not directly accessible in nn.Transformer)
        """
        # Note: torch.nn.Transformer doesn't expose attention weights directly
        # This is a limitation of the built-in implementation
        print("Warning: Attention weights not directly accessible with torch.nn.Transformer")
        return []


# For backward compatibility with the existing interface
EncoderOnlyTransformer = TorchTransformerEncoder


if __name__ == "__main__":
    # Test the model
    model = TorchTransformerEncoder(
        input_size=7,
        seq_length=30,
        d_model=128,
        nhead=8,
        num_encoder_layers=4,
        dim_feedforward=512,
        output_size=1,
        dropout=0.1,
        activation="gelu",
        positional_encoding=True,
        norm_type="layer",
        embedding_type="linear"
    )
    
    # Test input
    x = torch.randn(32, 30, 7)  # (batch_size, seq_length, input_size)
    pred, fv = model(x)
    print(f"Input shape: {x.shape}")
    print(f"Prediction shape: {pred.shape}")
    print(f"Feature vector shape: {fv.shape}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}") 