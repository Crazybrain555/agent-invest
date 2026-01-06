"""
PyTorch nn.Transformer Based Encoder-Only Classification Model

This module implements an encoder-only transformer model for multi-class classification
using PyTorch's built-in torch.nn.Transformer module. It maintains the same input 
structure as the regression model but outputs class probabilities.
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


class TorchTransformerClassifier(nn.Module):
    """
    Encoder-only transformer model for multi-class classification.
    
    This model takes a sequence of features and outputs class probabilities,
    making it suitable for cross-sectional stock classification tasks.
    """
    
    def __init__(self, input_size: int, seq_length: int, d_model: int, nhead: int, 
                 num_encoder_layers: int, dim_feedforward: int, num_classes: int,
                 dropout: float = 0.1, activation: str = "gelu", 
                 positional_encoding: bool = True, norm_type: str = "layer",
                 embedding_type: str = "linear", norm_first: bool = True,
                 pooling: str = "mean", feature_dim: Optional[int] = None):
        super().__init__()
        
        self.input_size = input_size
        self.seq_length = seq_length
        self.d_model = d_model
        self.num_classes = num_classes
        self.nhead = nhead
        self.num_encoder_layers = num_encoder_layers
        self.pooling = pooling
        
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
        
        # Feature dimension (intermediate representation)
        if feature_dim is None:
            feature_dim = d_model // 2  # Use d_model//2 as default feature dimension
        self.feature_dim = feature_dim
        
        # Feature projection layer (similar to regression model)
        self.feature_projection = nn.Sequential(
            nn.Linear(d_model, feature_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(feature_dim, feature_dim)
        )
        
        # Feature normalization (same as regression model for consistency)
        self.bn1 = nn.BatchNorm1d(feature_dim, affine=False, track_running_stats=True)
        
        # Classification head
        self.classifier = nn.Sequential(
            nn.Linear(feature_dim, feature_dim // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(feature_dim // 2, num_classes)
        )
        
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
        Forward pass of the classification transformer.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            src_mask: Optional source mask for transformer
            
        Returns:
            logits: Class logits tensor of shape (batch_size, num_classes)
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
        elif self.pooling == "first":
            # (seq_length, batch_size, d_model) -> (batch_size, d_model)
            x = x[0]
        elif self.pooling == "cls":
            # Use first token as CLS token (similar to BERT)
            x = x[0]
        else:
            raise ValueError(f"Unsupported pooling method: {self.pooling}")
        
        # Feature projection (similar to regression model)
        fv = self.feature_projection(x)  # [B, feature_dim]
        
        # Feature normalization (same as regression model)
        fv = self.bn1(fv)  # [B, feature_dim] - normalize feature distribution
        
        # Classification head
        logits = self.classifier(fv)  # [B, num_classes]
        
        return logits, fv
    
    def predict_proba(self, x: torch.Tensor, src_mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Get class probabilities using softmax.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            src_mask: Optional source mask for transformer
            
        Returns:
            Class probabilities tensor of shape (batch_size, num_classes)
        """
        logits, _ = self.forward(x, src_mask)
        return F.softmax(logits, dim=-1)
    
    def predict_log_proba(self, x: torch.Tensor, src_mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Get log class probabilities using log_softmax.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            src_mask: Optional source mask for transformer
            
        Returns:
            Log class probabilities tensor of shape (batch_size, num_classes)
        """
        logits, _ = self.forward(x, src_mask)
        return F.log_softmax(logits, dim=-1)
    
    def predict(self, x: torch.Tensor, src_mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Get predicted class labels.
        
        Args:
            x: Input tensor of shape (batch_size, seq_length, input_size)
            src_mask: Optional source mask for transformer
            
        Returns:
            Predicted class labels tensor of shape (batch_size,)
        """
        logits, _ = self.forward(x, src_mask)
        return torch.argmax(logits, dim=-1)
    
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


# For backward compatibility and alternative naming
EncoderOnlyTransformerClassifier = TorchTransformerClassifier
TransformerClassifier = TorchTransformerClassifier


if __name__ == "__main__":
    # Test the classification model
    print("Testing TorchTransformerClassifier...")
    
    # Test with different configurations
    configs = [
        {
            "name": "5-class quintile classification",
            "num_classes": 5,
            "input_size": 37,
            "batch_size": 32
        },
        {
            "name": "50-class fine-grained classification", 
            "num_classes": 50,
            "input_size": 37,
            "batch_size": 16
        }
    ]
    
    for config in configs:
        print(f"\n=== {config['name']} ===")
        
        model = TorchTransformerClassifier(
            input_size=config["input_size"],
            seq_length=30,
            d_model=128,
            nhead=8,
            num_encoder_layers=4,
            dim_feedforward=512,
            num_classes=config["num_classes"],
            dropout=0.1,
            activation="gelu",
            positional_encoding=True,
            norm_type="layer",
            embedding_type="linear",
            pooling="mean"
        )
        
        # Test input
        x = torch.randn(config["batch_size"], 30, config["input_size"])  # (batch_size, seq_length, input_size)
        
        # Test forward pass
        logits, fv = model(x)
        proba = model.predict_proba(x)
        log_proba = model.predict_log_proba(x)
        predictions = model.predict(x)
        
        print(f"Input shape: {x.shape}")
        print(f"Logits shape: {logits.shape}")
        print(f"Feature vector shape: {fv.shape}")
        print(f"Probabilities shape: {proba.shape}")
        print(f"Log probabilities shape: {log_proba.shape}")
        print(f"Predictions shape: {predictions.shape}")
        print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")
        
        # Verify probabilities sum to 1
        print(f"Probability sums (should be ~1.0): {proba.sum(dim=-1)[:5]}")
        print(f"Predicted classes range: [{predictions.min().item()}, {predictions.max().item()}]")
        
        # Test loss computation
        targets = torch.randint(0, config["num_classes"], (config["batch_size"],))
        loss_fn = nn.CrossEntropyLoss()
        loss = loss_fn(logits, targets)
        print(f"Cross-entropy loss: {loss.item():.4f}")
        
        # Test NLL loss with log probabilities
        nll_loss_fn = nn.NLLLoss()
        nll_loss = nll_loss_fn(log_proba, targets)
        print(f"NLL loss: {nll_loss.item():.4f}")
        
        print(f"Loss difference (should be ~0): {abs(loss.item() - nll_loss.item()):.6f}")
