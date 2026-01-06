#!/usr/bin/env python3
"""
Training script for PyTorch nn.Transformer Based Encoder-Only Classification Model

This script trains an encoder-only transformer model for classification using PyTorch's built-in
torch.nn.Transformer module instead of custom implementation. It maintains the same
input structure as the regression model but outputs class probabilities.

🚀 Enhanced Layer Monitoring Features:
- Comprehensive layer-wise activation and gradient monitoring via TrainingMonitor
- Classification-specific metrics: confidence, entropy, class distributions
- Real-time prediction analysis: logit statistics, probability distributions
- Batch-level monitoring: label balance, prediction certainty
- Epoch-level improvement tracking: accuracy/F1/IC deltas
- Advanced gradient tracking: pre/post-clip norms, clipping alerts

🚀 Return-based IC Calculation:
This script supports two methods for calculating Information Coefficient (IC):

1. Normalized Class-based IC (default):
   - Calculates correlation between normalized predicted class values [0,1] and actual class indices
   - Good for general classification tasks where class order has meaning

2. Return-based IC (new feature):
   - Calculates correlation between predicted expected returns and actual future returns
   - More directly measures prediction quality for financial return prediction tasks
   - Enable with: `use_return_based_ic: true` in training config

Example configuration:
```yaml
training:
  use_return_based_ic: true  # Enable return-based IC calculation
```

The return-based IC provides a more direct measure of how well the model predicts
actual return magnitudes, rather than just ordinal class relationships.
"""

import os
import sys
import yaml
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from pathlib import Path
import numpy as np
from typing import Dict, Any, Tuple, Optional, List
from collections import defaultdict
import warnings
from tqdm import tqdm
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, classification_report

# Add the src directory to the Python path
sys.path.append(str(Path(__file__).parent / "src"))

from src.models.transformer.torch.torch_classification_transformer import TorchTransformerClassifier
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders
from src.utils.experiment_utils import get_experiment_summary, create_experiment_dirs
from src.train.Neural_networks.RNN.DFZQ_GRU.training_monitor import TrainingMonitor


# ===============================================
# 🎯 Classification Loss Functions and Metrics
# ===============================================

def label_smoothing_cross_entropy(logits: torch.Tensor, labels: torch.Tensor, 
                                 smoothing: float = 0.1, num_classes: int = None) -> torch.Tensor:
    """
    Label smoothing cross entropy loss for classification.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
        smoothing: Label smoothing factor (0.0 = no smoothing, 1.0 = uniform)
        num_classes: Number of classes
    
    Returns:
        Loss tensor
    """
    if num_classes is None:
        num_classes = logits.size(-1)
    
    if smoothing == 0.0:
        return nn.functional.cross_entropy(logits, labels)
    
    confidence = 1.0 - smoothing
    log_probs = nn.functional.log_softmax(logits, dim=-1)
    
    # Convert labels to one-hot and apply smoothing
    true_dist = torch.zeros_like(log_probs)
    true_dist.fill_(smoothing / (num_classes - 1))
    true_dist.scatter_(1, labels.unsqueeze(1), confidence)
    
    return torch.mean(torch.sum(-true_dist * log_probs, dim=-1))


def focal_loss(logits: torch.Tensor, labels: torch.Tensor, 
               alpha: float = 1.0, gamma: float = 2.0) -> torch.Tensor:
    """
    Focal loss for handling class imbalance.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
        alpha: Weighting factor for rare class
        gamma: Focusing parameter
    
    Returns:
        Loss tensor
    """
    ce_loss = nn.functional.cross_entropy(logits, labels, reduction='none')
    pt = torch.exp(-ce_loss)
    focal_loss = alpha * (1 - pt) ** gamma * ce_loss
    return focal_loss.mean()


def ordinal_mse_loss(logits: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
    """
    Ordinal MSE loss treating classes as continuous values with normalization.
    
    For classification with numerical meaning (e.g., return quintiles),
    this penalizes distant predictions more than close ones.
    Normalizes class values to [0,1] to prevent large loss values.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1
    
    Returns:
        Loss tensor
    """
    # Convert logits to expected class value using softmax weights
    probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
    num_classes = logits.size(-1)
    
    # 🎯 Normalize class indices to [0, 1] range to prevent large loss values
    if num_classes == 1:
        # Edge case: single class
        predicted_values = torch.zeros_like(probs[:, 0])
        true_values = torch.zeros_like(labels.float())
    else:
        # Create normalized class indices [0, 1/(num_classes-1), 2/(num_classes-1), ..., 1]
        class_indices = torch.arange(num_classes, dtype=torch.float32, device=logits.device) / (num_classes - 1)
        
        # Calculate expected normalized class value: sum(normalized_class_index * probability)
        predicted_values = torch.sum(probs * class_indices.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Normalize true class values to [0, 1] range
        true_values = labels.float() / (num_classes - 1)  # [batch_size]
    
    # MSE between normalized predicted and true class values
    return nn.functional.mse_loss(predicted_values, true_values)


def ordinal_huber_loss(logits: torch.Tensor, labels: torch.Tensor, delta: float = 1.0) -> torch.Tensor:
    """
    Ordinal Huber loss - more robust to outliers than MSE with normalization.
    
    Combines L1 and L2 loss for ordinal classification where class distance matters.
    Normalizes class values to [0,1] to prevent large loss values.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1
        delta: Huber loss threshold (should be scaled for normalized range, e.g., 0.1)
    
    Returns:
        Loss tensor
    """
    # Convert logits to expected class value using softmax weights
    probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
    num_classes = logits.size(-1)
    
    # 🎯 Normalize class indices to [0, 1] range to prevent large loss values
    if num_classes == 1:
        # Edge case: single class
        predicted_values = torch.zeros_like(probs[:, 0])
        true_values = torch.zeros_like(labels.float())
    else:
        # Create normalized class indices [0, 1/(num_classes-1), 2/(num_classes-1), ..., 1]
        class_indices = torch.arange(num_classes, dtype=torch.float32, device=logits.device) / (num_classes - 1)
        
        # Calculate expected normalized class value: sum(normalized_class_index * probability)
        predicted_values = torch.sum(probs * class_indices.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Normalize true class values to [0, 1] range
        true_values = labels.float() / (num_classes - 1)  # [batch_size]
    
    # Huber loss between normalized predicted and true class values
    # Note: delta should be scaled appropriately for [0,1] range (e.g., 0.1 instead of 1.0)
    return nn.functional.huber_loss(predicted_values, true_values, delta=delta)


def distance_weighted_cross_entropy(logits: torch.Tensor, labels: torch.Tensor, 
                                   temperature: float = 1.0) -> torch.Tensor:
    """
    Cross entropy loss with distance-based weighting.
    
    Penalizes predictions that are far from the true class more heavily.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
        temperature: Controls how much to penalize distant predictions
    
    Returns:
        Loss tensor
    """
    batch_size, num_classes = logits.shape
    
    # Create distance matrix: |i - j| for all class pairs
    class_indices = torch.arange(num_classes, device=logits.device).float()
    distance_matrix = torch.abs(class_indices.unsqueeze(0) - class_indices.unsqueeze(1))  # [num_classes, num_classes]
    
    # Get distances from true labels to all classes
    label_distances = distance_matrix[labels]  # [batch_size, num_classes]
    
    # Create weights: distant classes get higher penalty (exponential decay)
    weights = torch.exp(label_distances * temperature)  # [batch_size, num_classes]
    
    # Apply weights to logits (increase penalty for distant classes)
    weighted_logits = logits - weights
    
    # Standard cross entropy on weighted logits
    return nn.functional.cross_entropy(weighted_logits, labels)


def earth_mover_distance_loss(logits: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
    """
    Earth Mover's Distance (Wasserstein) loss for ordinal classification.
    
    Measures the minimum cost to transform predicted distribution to true distribution.
    Particularly good for ordinal data where class order matters significantly.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
    
    Returns:
        Loss tensor
    """
    batch_size, num_classes = logits.shape
    
    # Convert logits to probabilities
    probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
    
    # Create one-hot targets
    targets = torch.zeros_like(probs)
    targets.scatter_(1, labels.unsqueeze(1), 1.0)  # [batch_size, num_classes]
    
    # Create distance matrix for class pairs
    class_indices = torch.arange(num_classes, device=logits.device).float()
    distance_matrix = torch.abs(class_indices.unsqueeze(0) - class_indices.unsqueeze(1))  # [num_classes, num_classes]
    
    # Calculate EMD for each sample in the batch
    batch_emd = torch.zeros(batch_size, device=logits.device)
    
    for i in range(batch_size):
        # EMD approximation: sum of absolute differences of cumulative distributions
        pred_cdf = torch.cumsum(probs[i], dim=0)
        true_cdf = torch.cumsum(targets[i], dim=0)
        batch_emd[i] = torch.sum(torch.abs(pred_cdf - true_cdf))
    
    return batch_emd.mean()


def combined_ordinal_classification_loss(logits: torch.Tensor, labels: torch.Tensor, 
                                        ordinal_weight: float = 0.5) -> torch.Tensor:
    """
    Combined loss mixing classification accuracy with ordinal ranking.
    
    Balances exact class prediction with close-class tolerance.
    Good for return quintiles where both exact accuracy and ranking matter.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
        ordinal_weight: Weight for ordinal component (0=pure classification, 1=pure ordinal)
    
    Returns:
        Loss tensor
    """
    # Classification component (exact accuracy)
    ce_loss = nn.functional.cross_entropy(logits, labels)
    
    # Ordinal component (ranking/distance)
    ordinal_loss = ordinal_mse_loss(logits, labels)
    
    # Combine losses
    return (1.0 - ordinal_weight) * ce_loss + ordinal_weight * ordinal_loss


def ordinal_ranking_loss(logits: torch.Tensor, labels: torch.Tensor, margin: float = 1.0) -> torch.Tensor:
    """
    Ranking loss for ordinal classification with normalization.
    
    Ensures that for any pair of samples, if true_label_i > true_label_j,
    then predicted_value_i > predicted_value_j (with margin).
    Normalizes values to [0,1] to prevent large loss values.
    
    Args:
        logits: Model outputs of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,)
        margin: Minimum margin between predictions (should be scaled for [0,1] range, e.g., 0.1)
    
    Returns:
        Loss tensor
    """
    batch_size, num_classes = logits.shape
    
    # 🎯 Normalize predicted and true values to [0, 1] range
    if num_classes == 1:
        # Edge case: single class
        predicted_values = torch.zeros(batch_size, device=logits.device)
        normalized_labels = torch.zeros_like(labels.float())
    else:
        # Convert logits to expected normalized class values
        probs = torch.softmax(logits, dim=-1)
        class_indices = torch.arange(num_classes, dtype=torch.float32, device=logits.device) / (num_classes - 1)
        predicted_values = torch.sum(probs * class_indices.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Normalize true labels to [0, 1] range
        normalized_labels = labels.float() / (num_classes - 1)  # [batch_size]
    
    # Create all pairs of samples
    pred_i = predicted_values.unsqueeze(1)  # [batch_size, 1]
    pred_j = predicted_values.unsqueeze(0)  # [1, batch_size]
    
    label_i = normalized_labels.unsqueeze(1)  # [batch_size, 1]
    label_j = normalized_labels.unsqueeze(0)  # [1, batch_size]
    
    # Calculate ranking violations
    # If label_i > label_j, then pred_i should be > pred_j + margin
    should_rank_higher = (label_i > label_j).float()  # [batch_size, batch_size]
    ranking_violations = torch.clamp(margin - (pred_i - pred_j), min=0.0)  # [batch_size, batch_size]
    
    # Only penalize violations where ranking should be higher
    ranking_loss = should_rank_higher * ranking_violations
    
    # Average over all valid pairs (exclude diagonal)
    mask = (1.0 - torch.eye(batch_size, device=logits.device))  # Exclude self-pairs
    ranking_loss = ranking_loss * mask
    
    return ranking_loss.sum() / (mask.sum() + 1e-8)  # Avoid division by zero


def spearman_ic_classification(logits: torch.Tensor, labels: torch.Tensor, num_classes: int) -> float:
    """
    Calculate Spearman IC for classification by normalizing both labels and predictions to [0,1].
    
    Args:
        logits: Model logits of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1
        num_classes: Number of classes
    
    Returns:
        Spearman correlation coefficient
    """
    try:
        # Convert logits to probabilities
        probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
        
        # Normalize true labels to [0, 1] range
        normalized_labels = labels.float() / (num_classes - 1)  # [batch_size]
        
        # Create normalized class values [0, 1/(num_classes-1), 2/(num_classes-1), ..., 1]
        class_values = torch.arange(num_classes, dtype=torch.float32, device=logits.device) / (num_classes - 1)
        
        # Calculate expected normalized class value: sum(class_value * probability)
        normalized_preds = torch.sum(probs * class_values.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Convert to numpy for correlation calculation
        pred_np = normalized_preds.detach().cpu().numpy().flatten()
        label_np = normalized_labels.detach().cpu().numpy().flatten()
        
        # Calculate Spearman correlation
        from scipy.stats import spearmanr
        corr_stat, _ = spearmanr(pred_np, label_np, nan_policy="omit")
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0
    except Exception:
        return 0.0


def spearman_ic_classification_returns(logits: torch.Tensor, labels: torch.Tensor, 
                                     original_returns: torch.Tensor, num_classes: int) -> float:
    """
    Calculate Spearman IC for classification using actual return values.
    
    This calculates the correlation between predicted expected returns (based on class probabilities)
    and actual future returns, providing a more direct measure of prediction quality.
    
    Args:
        logits: Model logits of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1 (not used directly)
        original_returns: Actual return values of shape (batch_size,)
        num_classes: Number of classes
    
    Returns:
        Spearman correlation coefficient between predicted expected returns and actual returns
    """
    try:
        # Convert logits to probabilities
        probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
        
        # Calculate predicted expected returns using class probabilities
        # We need to map each class back to its representative return value
        # For simplicity, we'll use the mean return of each class from the batch
        # In practice, this could be pre-computed from training data
        
        # Group original returns by predicted class to get class representative values
        predicted_classes = torch.argmax(logits, dim=-1)  # [batch_size]
        class_return_means = torch.zeros(num_classes, device=logits.device)
        
        for class_idx in range(num_classes):
            class_mask = labels == class_idx
            if class_mask.sum() > 0:
                class_return_means[class_idx] = original_returns[class_mask].mean()
            else:
                # If no samples for this class, use 0 or interpolate
                class_return_means[class_idx] = 0.0
        
        # Calculate expected returns using probabilities and class representative returns
        predicted_expected_returns = torch.sum(probs * class_return_means.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Convert to numpy for correlation calculation
        pred_returns_np = predicted_expected_returns.detach().cpu().numpy().flatten()
        actual_returns_np = original_returns.detach().cpu().numpy().flatten()
        
        # Remove NaN/Inf values
        valid_mask = np.isfinite(pred_returns_np) & np.isfinite(actual_returns_np)
        if np.sum(valid_mask) < 2:
            return 0.0
        
        # Calculate Spearman correlation
        from scipy.stats import spearmanr
        corr_stat, _ = spearmanr(pred_returns_np[valid_mask], actual_returns_np[valid_mask], nan_policy="omit")
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0
    except Exception:
        return 0.0


def pearson_ic_classification(logits: torch.Tensor, labels: torch.Tensor, num_classes: int) -> float:
    """
    Calculate Pearson IC for classification by normalizing both labels and predictions to [0,1].
    
    Args:
        logits: Model logits of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1
        num_classes: Number of classes
    
    Returns:
        Pearson correlation coefficient
    """
    try:
        # Convert logits to probabilities
        probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
        
        # Normalize true labels to [0, 1] range
        normalized_labels = labels.float() / (num_classes - 1)  # [batch_size]
        
        # Create normalized class values [0, 1/(num_classes-1), 2/(num_classes-1), ..., 1]
        class_values = torch.arange(num_classes, dtype=torch.float32, device=logits.device) / (num_classes - 1)
        
        # Calculate expected normalized class value: sum(class_value * probability)
        normalized_preds = torch.sum(probs * class_values.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Convert to numpy for correlation calculation
        pred_np = normalized_preds.detach().cpu().numpy().flatten()
        label_np = normalized_labels.detach().cpu().numpy().flatten()
        
        # Remove NaN values
        valid_mask = ~(np.isnan(pred_np) | np.isnan(label_np))
        if np.sum(valid_mask) < 2:
            return 0.0
        
        # Calculate Pearson correlation
        from scipy.stats import pearsonr
        corr_stat, _ = pearsonr(pred_np[valid_mask], label_np[valid_mask])
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0
    except Exception:
        return 0.0


def pearson_ic_classification_returns(logits: torch.Tensor, labels: torch.Tensor, 
                                    original_returns: torch.Tensor, num_classes: int) -> float:
    """
    Calculate Pearson IC for classification using actual return values.
    
    This calculates the correlation between predicted expected returns (based on class probabilities)
    and actual future returns, providing a more direct measure of prediction quality.
    
    Args:
        logits: Model logits of shape (batch_size, num_classes)
        labels: True labels of shape (batch_size,) with values 0 to num_classes-1 (not used directly)
        original_returns: Actual return values of shape (batch_size,)
        num_classes: Number of classes
    
    Returns:
        Pearson correlation coefficient between predicted expected returns and actual returns
    """
    try:
        # Convert logits to probabilities
        probs = torch.softmax(logits, dim=-1)  # [batch_size, num_classes]
        
        # Calculate predicted expected returns using class probabilities
        # We need to map each class back to its representative return value
        # For simplicity, we'll use the mean return of each class from the batch
        # In practice, this could be pre-computed from training data
        
        # Group original returns by predicted class to get class representative values
        predicted_classes = torch.argmax(logits, dim=-1)  # [batch_size]
        class_return_means = torch.zeros(num_classes, device=logits.device)
        
        for class_idx in range(num_classes):
            class_mask = labels == class_idx
            if class_mask.sum() > 0:
                class_return_means[class_idx] = original_returns[class_mask].mean()
            else:
                # If no samples for this class, use 0 or interpolate
                class_return_means[class_idx] = 0.0
        
        # Calculate expected returns using probabilities and class representative returns
        predicted_expected_returns = torch.sum(probs * class_return_means.unsqueeze(0), dim=-1)  # [batch_size]
        
        # Convert to numpy for correlation calculation
        pred_returns_np = predicted_expected_returns.detach().cpu().numpy().flatten()
        actual_returns_np = original_returns.detach().cpu().numpy().flatten()
        
        # Remove NaN/Inf values
        valid_mask = np.isfinite(pred_returns_np) & np.isfinite(actual_returns_np)
        if np.sum(valid_mask) < 2:
            return 0.0
        
        # Calculate Pearson correlation
        from scipy.stats import pearsonr
        corr_stat, _ = pearsonr(pred_returns_np[valid_mask], actual_returns_np[valid_mask])
        return float(corr_stat) if not np.isnan(corr_stat) else 0.0
    except Exception:
        return 0.0


def calculate_classification_metrics(predictions: torch.Tensor, labels: torch.Tensor, 
                                   num_classes: int, logits: torch.Tensor = None, 
                                   original_returns: torch.Tensor = None,
                                   use_return_based_ic: bool = False) -> Dict[str, float]:
    """
    Calculate classification metrics including IC measures.
    
    Args:
        predictions: Predicted class labels of shape (batch_size,)
        labels: True labels of shape (batch_size,)
        num_classes: Number of classes
        logits: Model logits of shape (batch_size, num_classes) for IC calculation
        original_returns: Original return values of shape (batch_size,) for return-based IC
        use_return_based_ic: Whether to use return-based IC calculation instead of normalized class IC
    
    Returns:
        Dictionary of metrics
    """
    predictions_np = predictions.detach().cpu().numpy()
    labels_np = labels.detach().cpu().numpy()
    
    # Basic metrics
    accuracy = accuracy_score(labels_np, predictions_np)
    
    # Use different averaging strategies based on task type
    if num_classes == 2:
        # Binary classification
        f1 = f1_score(labels_np, predictions_np, average='binary')
        precision = precision_score(labels_np, predictions_np, average='binary', zero_division=0)
        recall = recall_score(labels_np, predictions_np, average='binary', zero_division=0)
    else:
        # Multi-class classification
        f1 = f1_score(labels_np, predictions_np, average='macro', zero_division=0)
        precision = precision_score(labels_np, predictions_np, average='macro', zero_division=0)
        recall = recall_score(labels_np, predictions_np, average='macro', zero_division=0)
    
    metrics = {
        'accuracy': float(accuracy),
        'f1_score': float(f1),
        'precision': float(precision),
        'recall': float(recall)
    }
    
    # Add IC metrics if logits are provided
    if logits is not None:
        if use_return_based_ic and original_returns is not None:
            # Use return-based IC calculation
            metrics['pearson_ic'] = pearson_ic_classification_returns(logits, labels, original_returns, num_classes)
            metrics['spearman_ic'] = spearman_ic_classification_returns(logits, labels, original_returns, num_classes)
        else:
            # Use normalized class-based IC calculation (default)
            metrics['pearson_ic'] = pearson_ic_classification(logits, labels, num_classes)
            metrics['spearman_ic'] = spearman_ic_classification(logits, labels, num_classes)
    
    return metrics


class ConfigLoader:
    """Load and validate configuration from YAML file."""
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self._resolve_preset()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config
    
    def _resolve_preset(self):
        """Resolve preset configuration if specified."""
        preset = self.config.get('preset', 'medium')
        presets = self.config.get('presets', {})
        
        if preset in presets:
            print(f"📋 Using preset: {preset} - {presets[preset].get('description', '')}")
            preset_config = presets[preset]
            
            # Merge preset configuration with base configuration
            if 'architecture' in preset_config:
                self.config['architecture'] = preset_config['architecture']
            if 'training' in preset_config:
                self.config['training'] = preset_config['training']
            if 'data' in preset_config:
                # Merge data configuration, preserving existing settings
                if 'data' not in self.config:
                    self.config['data'] = {}
                self.config['data'].update(preset_config['data'])
        elif preset != 'custom':
            print(f"⚠️  Warning: Preset '{preset}' not found, using custom configuration")
    
    def get_architecture_config(self) -> Dict[str, Any]:
        """Get architecture configuration."""
        return self.config.get('architecture', {})
    
    def get_training_config(self) -> Dict[str, Any]:
        """Get training configuration."""
        return self.config.get('training', {})
    
    def get_data_config(self) -> Dict[str, Any]:
        """Get data configuration."""
        return self.config.get('data', {})
    
    def get_saving_config(self) -> Dict[str, Any]:
        """Get saving configuration."""
        return self.config.get('saving', {})
    
    def get_preset_name(self) -> str:
        """Get the current preset name."""
        return self.config.get('preset', 'custom')


class TorchTransformerClassificationTrainer:
    """Trainer class for the PyTorch nn.Transformer based encoder-only classification model."""
    
    def __init__(self, config_path: str):
        self.config_loader = ConfigLoader(config_path)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load configurations
        self.arch_config = self.config_loader.get_architecture_config()
        self.train_config = self.config_loader.get_training_config()
        self.data_config = self.config_loader.get_data_config()
        self.save_config = self.config_loader.get_saving_config()
        
        # Get preset name for logging (needed early for setup methods)
        self.preset_name = self.config_loader.get_preset_name()
        
        # Classification specific configuration
        self.num_classes = self.arch_config['num_classes']
        self.class_weights = self.train_config.get('class_weights', None)
        self.label_smoothing = self.train_config.get('label_smoothing', 0.0)
        
        # Traditional classification losses
        self.use_focal_loss = self.train_config.get('use_focal_loss', False)
        self.focal_alpha = self.train_config.get('focal_alpha', 1.0)
        self.focal_gamma = self.train_config.get('focal_gamma', 2.0)
        
        # 🎯 Ordinal-aware losses for numerical class labels (e.g., return quintiles)
        self.loss_type = self.train_config.get('loss_type', 'cross_entropy')  # Available: cross_entropy, ordinal_mse, ordinal_huber, distance_weighted_ce, earth_mover_distance, combined_ordinal, ordinal_ranking
        self.ordinal_huber_delta = self.train_config.get('ordinal_huber_delta', 1.0)
        self.distance_ce_temperature = self.train_config.get('distance_ce_temperature', 1.0)
        self.ordinal_weight = self.train_config.get('ordinal_weight', 0.5)  # For combined_ordinal loss
        self.ranking_margin = self.train_config.get('ranking_margin', 1.0)  # For ordinal_ranking loss
        
        # 🚀 Classification label configuration
        self.classification_label_name = self.data_config.get('classification_label_name', None)
        if not self.classification_label_name:
            # Generate default classification label name based on number of classes
            self.classification_label_name = f'classification_label_{self.num_classes}'
            print(f"🎯 Auto-generated classification label name: {self.classification_label_name}")
        
        # 🚀 IC calculation configuration
        self.use_return_based_ic = self.train_config.get('use_return_based_ic', False)
        self.return_original_values = self.use_return_based_ic  # Enable original returns when using return-based IC
        if self.use_return_based_ic:
            print(f"📊 Using return-based IC calculation (correlation with actual returns)")
        else:
            print(f"📊 Using normalized class-based IC calculation (correlation with class indices)")
        
        # 🚀 特征选择配置
        self.selected_factors = self.data_config.get('selected_factors', None)
        
        # Create experiment config object for consistent directory structure
        self._create_experiment_config()
        
        # Setup experiment directories
        self._setup_experiment_directories()
        
        # Create dataloaders first to get actual input size
        self.train_loader, self.valid_loader, self.test_loader = self._create_dataloaders()
        
        # Create model
        self.model = self._create_model()
        self.model.to(self.device)
        
        # Setup training components
        self.optimizer = self._create_optimizer()
        self.scheduler = self._create_scheduler()
        self.loss_fn = self._create_loss_function()
        
        # Gradient clipping threshold
        self.grad_clip_norm = float(self.train_config.get('grad_clip_norm', 1.0))
        
        # Noise configuration
        self.noise_config = self.train_config.get('noise', {})
        self.noise_enabled = self.noise_config.get('enabled', False)
        self.noise_std = self.noise_config.get('gaussian_std', 0.01)
        self.noise_apply_to_features = self.noise_config.get('apply_to_features', True)
        
        # Training state
        self.current_epoch = 0
        self.best_val_acc = 0.0  # Track highest accuracy
        self.patience_counter = 0
        
        # Setup logging
        self.writer = self._setup_logging()
        
        # Setup TrainingMonitor for comprehensive layer monitoring
        self.monitor = self._setup_training_monitor()
        print("📊 Using comprehensive layer monitoring with TrainingMonitor")

        # Keyboard control flags and listener
        self._stop_training_requested = False
        self._test_on_stop_requested = False
        self._start_keyboard_monitor()
        
        # Final device verification after initialization
        self._verify_device_setup()
        
        print(f"✅ TorchTransformerClassificationTrainer initialized successfully!")
        print(f"   Device: {self.device}")
        print(f"   CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"   CUDA device count: {torch.cuda.device_count()}")
            print(f"   Current CUDA device: {torch.cuda.current_device()}")
        print(f"   Model device: {next(self.model.parameters()).device}")
        print(f"   Preset: {self.preset_name}")
        print(f"   Experiment directory: {self.output_dir}")
        print(f"   Model parameters: {sum(p.numel() for p in self.model.parameters()):,}")
        print(f"   Number of classes: {self.num_classes}")
        print(f"   🎯 Classification label: {self.classification_label_name}")
        print(f"   Train batches: {len(self.train_loader)}")
        print(f"   Valid batches: {len(self.valid_loader)}")
        print(f"   Test batches: {len(self.test_loader)}")
        if self.selected_factors:
            print(f"   📊 Selected factors: {len(self.selected_factors)} factors")
        else:
            print(f"   📊 Using all available factors")
        print(f"   Monitoring: Comprehensive layer monitoring + Classification metrics (accuracy, F1, precision, recall)")
        if self.use_return_based_ic:
            print(f"   IC Calculation: Return-based (correlation with actual future returns)")
        else:
            print(f"   IC Calculation: Normalized class-based (correlation with class indices)")
        print(f"   Noise augmentation: {'Enabled' if self.noise_enabled else 'Disabled'}")
        if self.noise_enabled:
            print(f"     - Gaussian std: {self.noise_std}")
            print(f"     - Apply to features: {self.noise_apply_to_features}")
        print(f"   Loss function: {self._get_loss_function_name()}")
        print(f"   Layer Monitoring: Activations, gradients, weights distribution tracking")
    
    def _verify_device_setup(self):
        """Verify and fix device setup for all components."""
        # Check CUDA availability
        cuda_available = torch.cuda.is_available()
        expected_device = torch.device("cuda" if cuda_available else "cpu")
        
        # Update device if needed
        if expected_device != self.device:
            print(f"⚠️  Device mismatch: expected {expected_device}, got {self.device}")
            self.device = expected_device
        
        # Ensure model is on correct device
        model_device = next(self.model.parameters()).device
        if model_device != self.device:
            print(f"🔧 Moving model from {model_device} to {self.device}")
            self.model = self.model.to(self.device)
        
        # Verify loss function is created with current device context
        self.loss_fn = self._create_loss_function()
        
        print(f"✅ Device setup verified: all components on {self.device}")
    
    def _create_experiment_config(self):
        """Create experiment config object for consistent directory structure."""
        class ExperimentConfig:
            def __init__(self, arch_config, train_config, data_config, save_config, preset_name, selected_factors):
                # Map transformer config to GRU-style naming for experiment utils
                self.hidden_size = arch_config['d_model']
                self.num_layers = arch_config['num_encoder_layers']
                self.lr = train_config['optimizer']['learning_rate']
                self.attention = True
                self.bidirectional = False
                
                # Dataset and output configuration
                self.dataset_path = data_config['dataset_path']
                self.output_root = save_config['save_dir']
                self.experiment_name = f"TorchTransformerClassification_{preset_name}"
                self.auto_timestamp = True
                self.output_format = "{base}_{exp_name}_{timestamp}"
                
                # Date ranges configuration
                self.date_ranges = data_config.get('date_ranges', None)
                self.use_custom_splits = data_config.get('use_custom_splits', False)
                
                # Feature selection configuration
                self.selected_factors = selected_factors
                
                # Update output directory to include date range information
                if self.date_ranges and self.use_custom_splits:
                    valid_range = self.date_ranges.get("valid", ["", ""])
                    train_range = self.date_ranges.get("train", ["", ""])
                    self.output_root = f"outputs/encoder_only_transformer_classification_vd_{valid_range[0]}_{valid_range[1]}_t_{train_range[0]}_{train_range[1]}"
                
                # Include feature information in output directory
                if self.selected_factors:
                    factors_suffix = f"_factors_{len(self.selected_factors)}"
                    self.output_root += factors_suffix
        
        self.experiment_config = ExperimentConfig(
            self.arch_config, 
            self.train_config, 
            self.data_config, 
            self.save_config,
            self.config_loader.get_preset_name(),
            self.selected_factors
        )
    
    def _setup_experiment_directories(self):
        """Setup experiment directories."""
        experiment_info = get_experiment_summary(self.experiment_config)
        self.output_dir = experiment_info['output_dir']
        self.run_name = experiment_info['run_name']
        
        # Create experiment directories
        self.dirs = create_experiment_dirs(self.output_dir)
        self.ckpt_dir = Path(self.dirs['ckpt'])
        self.log_dir = Path(self.dirs['logs'])
        self.bt_dir = Path(self.dirs['bt_results'])
        
        print(f"📁 Experiment directory: {self.output_dir}")
        print(f"📊 Log directory: {self.log_dir}")
        print(f"💾 Checkpoint directory: {self.ckpt_dir}")
        
        if hasattr(self.experiment_config, 'date_ranges') and self.experiment_config.date_ranges:
            print(f"🗓️  Date ranges: {self.experiment_config.date_ranges}")
        
        if hasattr(self.experiment_config, 'selected_factors') and self.experiment_config.selected_factors:
            print(f"🎯  Selected factors: {len(self.experiment_config.selected_factors)} factors")
    
    def _create_dataloaders(self):
        """Create train/validation/test dataloaders."""
        # Prepare dataloader config
        dataloader_config = {
            "dataset_path": self.data_config['dataset_path'],
            "batch_size": self.train_config['batch_size'],
            "num_workers": 4,
            "seed": 42,
            "chunk_size": self.data_config['chunk_size'],
            "memory_limit": self.data_config['memory_limit'],
            "use_fixed_indices": self.data_config['use_fixed_indices'],
            "reverse_seq": self.data_config['reverse_seq'],
            "use_custom_splits": self.data_config.get('use_custom_splits', False),
            "date_ranges": self.data_config.get('date_ranges', None),
            "selected_factors": self.selected_factors,
            "duck_threads": self.data_config.get('duck_threads', 16),
            "duck_memory": self.data_config.get('duck_memory', '16GB'),
            "duck_cache": self.data_config.get('duck_cache', '4GB'),
            "prefetch_factor": self.data_config.get('prefetch_factor', 4)
        }
        
        print(f"📊 Creating dataloaders with config: {dataloader_config}")
        
        train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
            config=dataloader_config,
            keep_meta_train=False,
            keep_meta_eval=False,
            use_fixed_indices=dataloader_config['use_fixed_indices'],
            selected_factors=dataloader_config['selected_factors'],
            # 🚀 Classification-specific parameters
            label_type='classification',
            classification_label_name=self.classification_label_name,
            return_original_values=self.return_original_values  # 🚀 Enable original returns for IC calculation
        )
        
        return train_loader, valid_loader, test_loader
    
    def _create_model(self) -> TorchTransformerClassifier:
        """Create the PyTorch nn.Transformer based encoder-only classification model."""
        model = TorchTransformerClassifier(
            input_size=self.arch_config['input_size'],
            seq_length=self.arch_config['seq_length'],
            d_model=self.arch_config['d_model'],
            nhead=self.arch_config['nhead'],
            num_encoder_layers=self.arch_config['num_encoder_layers'],
            dim_feedforward=self.arch_config['dim_feedforward'],
            num_classes=self.arch_config['num_classes'],
            dropout=self.arch_config['dropout'],
            activation=self.arch_config['activation'],
            positional_encoding=self.arch_config['positional_encoding'],
            embedding_type=self.arch_config['embedding_type'],
            norm_type=self.arch_config.get('norm_type', 'layer'),
            norm_first=self.arch_config.get('norm_first', True),
            pooling=self.arch_config.get('pooling', 'mean'),
            feature_dim=self.arch_config.get('feature_dim', None)
        )
        return model
    
    def _create_optimizer(self) -> optim.Optimizer:
        """Create optimizer."""
        opt_config = self.train_config['optimizer']
        
        learning_rate = float(opt_config['learning_rate'])
        weight_decay = float(opt_config['weight_decay'])
        beta1 = float(opt_config['beta1'])
        beta2 = float(opt_config['beta2'])
        
        if opt_config['type'] == "Adam":
            return optim.Adam(
                self.model.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay,
                betas=(beta1, beta2)
            )
        elif opt_config['type'] == "AdamW":
            return optim.AdamW(
                self.model.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay,
                betas=(beta1, beta2)
            )
        else:
            raise ValueError(f"Unsupported optimizer: {opt_config['type']}")
    
    def _create_scheduler(self):
        """Create learning rate scheduler."""
        scheduler_config = self.train_config.get('scheduler', {})
        if not scheduler_config:
            return None
            
        scheduler_type = scheduler_config.get('type', 'ReduceLROnPlateau')
        
        factor = float(scheduler_config.get('factor', 0.9))
        patience = int(scheduler_config.get('patience', 10))
        min_lr = float(scheduler_config.get('min_lr', 1e-7))
        
        if scheduler_type == 'ReduceLROnPlateau':
            return optim.lr_scheduler.ReduceLROnPlateau(
                self.optimizer,
                mode='max',  # For accuracy maximization
                factor=factor,
                patience=patience,
                min_lr=min_lr
            )
        elif scheduler_type == 'CosineAnnealingLR':
            return optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=self.train_config['epochs'] - self.train_config['scheduler']['lr_scheduler_warmup_epochs'],
                eta_min=min_lr
            )
        elif scheduler_type == 'warm_cos':
            warmup_scheduler = optim.lr_scheduler.LinearLR(
                self.optimizer,
                start_factor=0.1,
                end_factor=1.0,
                total_iters=self.train_config['scheduler']['lr_scheduler_warmup_epochs']
            )
            cosine_scheduler = optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=self.train_config['epochs'] - self.train_config['scheduler']['lr_scheduler_warmup_epochs'],
                eta_min=min_lr
            )
            scheduler = optim.lr_scheduler.SequentialLR(
                self.optimizer,
                schedulers=[warmup_scheduler, cosine_scheduler],
                milestones=[self.train_config['scheduler']['lr_scheduler_warmup_epochs']]
            )
            return scheduler
        else:
            print(f"⚠️  Warning: Unknown scheduler type '{scheduler_type}', using no scheduler")
            return None
    
    def _create_loss_function(self):
        """Create loss function with support for ordinal-aware losses."""
        
        # 🎯 Ordinal-aware losses for numerical class labels (return quintiles/percentiles)
        if self.loss_type == 'ordinal_mse':
            return lambda logits, labels: ordinal_mse_loss(logits, labels)
        
        elif self.loss_type == 'ordinal_huber':
            return lambda logits, labels: ordinal_huber_loss(logits, labels, self.ordinal_huber_delta)
        
        elif self.loss_type == 'distance_weighted_ce':
            return lambda logits, labels: distance_weighted_cross_entropy(logits, labels, self.distance_ce_temperature)
        
        elif self.loss_type == 'earth_mover_distance':
            return lambda logits, labels: earth_mover_distance_loss(logits, labels)
        
        elif self.loss_type == 'combined_ordinal':
            return lambda logits, labels: combined_ordinal_classification_loss(logits, labels, self.ordinal_weight)
        
        elif self.loss_type == 'ordinal_ranking':
            return lambda logits, labels: ordinal_ranking_loss(logits, labels, self.ranking_margin)
        
        # Traditional classification losses
        elif self.use_focal_loss:
            return lambda logits, labels: focal_loss(logits, labels, self.focal_alpha, self.focal_gamma)
        
        elif self.label_smoothing > 0:
            return lambda logits, labels: label_smoothing_cross_entropy(
                logits, labels, self.label_smoothing, self.num_classes)
        
        else:
            # Standard cross entropy loss (default)
            weights = None
            if self.class_weights:
                weights = torch.tensor(self.class_weights, dtype=torch.float32, device=self.device)
            return nn.CrossEntropyLoss(weight=weights)
    
    def _get_loss_function_name(self) -> str:
        """Get human-readable loss function name."""
        if self.loss_type == 'ordinal_mse':
            return "Ordinal MSE Loss"
        elif self.loss_type == 'ordinal_huber':
            return f"Ordinal Huber Loss (δ={self.ordinal_huber_delta})"
        elif self.loss_type == 'distance_weighted_ce':
            return f"Distance-Weighted Cross Entropy (T={self.distance_ce_temperature})"
        elif self.loss_type == 'earth_mover_distance':
            return "Earth Mover's Distance Loss"
        elif self.loss_type == 'combined_ordinal':
            return f"Combined Ordinal Loss (weight={self.ordinal_weight})"
        elif self.loss_type == 'ordinal_ranking':
            return f"Ordinal Ranking Loss (margin={self.ranking_margin})"
        elif self.use_focal_loss:
            return f"Focal Loss (α={self.focal_alpha}, γ={self.focal_gamma})"
        elif self.label_smoothing > 0:
            return f"Label Smoothing Cross Entropy (smoothing={self.label_smoothing})"
        else:
            return "Cross Entropy Loss"
    
    def _setup_logging(self) -> SummaryWriter:
        """Setup TensorBoard logging."""
        use_experiment_dir = self.save_config.get('tensorboard_in_experiment_dir', True)
        
        if use_experiment_dir:
            tensorboard_log_dir = self.log_dir
            print(f"📊 TensorBoard logs will be saved to experiment directory: {tensorboard_log_dir}")
        else:
            current_dir = Path(__file__).parent
            parent_parent_dir = current_dir.parent.parent
            tf_logs_dir = parent_parent_dir / "tf-logs"
            tf_logs_dir.mkdir(parents=True, exist_ok=True)
            tensorboard_log_dir = tf_logs_dir / self.run_name
            print(f"📊 TensorBoard logs will be saved to tf-logs directory: {tensorboard_log_dir}")
        
        writer = SummaryWriter(
            log_dir=str(tensorboard_log_dir),
            comment=f"_{self.preset_name}_torch_transformer_classification"
        )
        
        # Log hyperparameters
        hparams = {
            'preset': self.preset_name,
            'd_model': self.arch_config['d_model'],
            'num_encoder_layers': self.arch_config['num_encoder_layers'],
            'nhead': self.arch_config['nhead'],
            'batch_size': self.train_config['batch_size'],
            'learning_rate': self.train_config['optimizer']['learning_rate'],
            'dropout': self.arch_config['dropout'],
            'num_classes': self.num_classes,
            'label_smoothing': self.label_smoothing,
            'use_focal_loss': self.use_focal_loss,
            'noise_enabled': self.noise_enabled,
            'noise_std': self.noise_std if self.noise_enabled else 0.0
        }
        try:
            import yaml as _yaml_for_tb
            hparam_text = _yaml_for_tb.dump(hparams, default_flow_style=False, sort_keys=False)
        except Exception:
            hparam_text = str(hparams)
        writer.add_text('hparams', f"``\n{hparam_text}\n``")
        
        return writer
    
    def _setup_training_monitor(self) -> TrainingMonitor:
        """Setup training monitor for comprehensive layer monitoring."""
        monitor_config = {
            'batch_log_freq': 200,
            'detailed_log_freq': 500,
            'monitor_distributions': True,
            'monitor_gradients_detailed': True,
            'monitor_layer_selection': 'key_layers',
            'ema_decay': 0.95,
            'act_zero_epsilon': 1e-3
        }
        
        monitor = TrainingMonitor(self.writer, monitor_config)
        monitor.setup_model_monitoring(self.model)
        
        return monitor

    def _keyboard_listener(self):
        """Simple keyboard listener for interactive control."""
        try:
            print("\n[Keyboard] Controls: 'q'+Enter stop, 't'+Enter stop+test, 'c'+Enter cancel")
            import sys
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == 'q':
                    self._stop_training_requested = True
                    self._test_on_stop_requested = False
                    print("[Keyboard] Stop requested (no test)")
                elif cmd == 't':
                    self._stop_training_requested = True
                    self._test_on_stop_requested = True
                    print("[Keyboard] Stop+Test requested")
                elif cmd == 'c':
                    self._stop_training_requested = False
                    self._test_on_stop_requested = False
                    print("[Keyboard] Stop canceled")
        except Exception:
            pass

    def _start_keyboard_monitor(self):
        """Start the keyboard monitor thread (non-blocking)."""
        import threading
        t = threading.Thread(target=self._keyboard_listener, daemon=True)
        t.start()
    

    
    def _add_gaussian_noise(self, tensor: torch.Tensor, std: float) -> torch.Tensor:
        """Add gaussian noise to a tensor."""
        if not self.noise_enabled or std <= 0:
            return tensor
        
        noise = torch.randn_like(tensor) * std
        return tensor + noise
    
    def train_epoch(self) -> Tuple[float, float, float, float, float, float, float]:
        """Train for one epoch."""
        self.model.train()
        
        # Training statistics
        epoch_losses = []
        epoch_accuracies = []
        epoch_f1_scores = []
        epoch_grad_norms = []
        all_predictions = []
        all_labels = []
        all_logits = []  # For epoch-level IC calculation
        all_original_returns = []  # For epoch-level return-based IC calculation
        
        pbar = tqdm(self.train_loader, desc='Train', leave=False)
        early_stop_triggered = False
        
        for batch_idx, batch in enumerate(pbar):
            # Extract features, labels, and optional original returns
            if len(batch) == 5:  # With metadata and original returns
                features, labels, original_returns, _, _ = batch
            elif len(batch) == 4:  # With metadata
                features, labels, _, _ = batch
                original_returns = None
            elif len(batch) == 3:  # Without metadata but with original returns
                features, labels, original_returns = batch
            else:  # Without metadata and original returns
                features, labels = batch
                original_returns = None
            
            # Move to device and prepare
            features = features.to(self.device)
            labels = labels.to(self.device).long()  # Classification labels should be long
            if original_returns is not None:
                original_returns = original_returns.to(self.device)
            
            # Apply gaussian noise during training (if enabled)
            if self.noise_enabled and self.noise_apply_to_features:
                features = self._add_gaussian_noise(features, self.noise_std)
            
            # Forward pass
            self.optimizer.zero_grad()
            logits, fv = self.model(features)  # [B, num_classes], [B, feature_dim]
            
            # Calculate loss
            loss = self.loss_fn(logits, labels)
            
            # Backward pass
            loss.backward()

            # Gradient clipping
            grad_norm = torch.nn.utils.clip_grad_norm_(
                self.model.parameters(), max_norm=self.grad_clip_norm
            )
            grad_norm_value = float(grad_norm.item() if hasattr(grad_norm, 'item') else grad_norm)

            # Update weights
            self.optimizer.step()
            
            # Calculate predictions and metrics
            predictions = torch.argmax(logits, dim=-1)
            batch_metrics = calculate_classification_metrics(
                predictions, labels, self.num_classes, logits, 
                original_returns, self.use_return_based_ic
            )
            
            # Accumulate statistics
            epoch_losses.append(loss.item())
            epoch_accuracies.append(batch_metrics['accuracy'])
            epoch_f1_scores.append(batch_metrics['f1_score'])
            epoch_grad_norms.append(grad_norm_value)
            
            # Collect for epoch-level metrics
            all_predictions.extend(predictions.cpu().numpy())
            all_labels.extend(labels.cpu().numpy())
            all_logits.append(logits.detach().cpu())  # Store logits for epoch-level IC
            if original_returns is not None:
                all_original_returns.append(original_returns.detach().cpu())  # Store original returns for epoch-level IC
            
            # Batch-level TensorBoard logging
            if self.writer is not None:
                global_step = (self.current_epoch - 1) * len(self.train_loader) + batch_idx
                self.writer.add_scalar('Train_Batch/loss', loss.item(), global_step)
                self.writer.add_scalar('Train_Batch/accuracy', batch_metrics['accuracy'], global_step)
                self.writer.add_scalar('Train_Batch/f1_score', batch_metrics['f1_score'], global_step)
                self.writer.add_scalar('Train_Batch/pearson_ic', batch_metrics['pearson_ic'], global_step)
                self.writer.add_scalar('Train_Batch/spearman_ic', batch_metrics['spearman_ic'], global_step)
                self.writer.add_scalar('Train_Batch/grad_norm', grad_norm_value, global_step)
            
            # 🚀 Comprehensive layer monitoring using TrainingMonitor
            if self.monitor:
                # Comprehensive monitoring: Core + LayerDiag + Alerts
                self.monitor.monitor_comprehensive(
                    model=self.model,
                    loss=loss.item(),
                    grad_norm=grad_norm_value, 
                    lr=self.optimizer.param_groups[0]['lr'],
                    predictions=predictions.float().unsqueeze(-1),  # Convert to [B, 1] for monitor compatibility
                    labels=labels.float().unsqueeze(-1),  # Convert to [B, 1] for monitor compatibility
                    step=global_step,
                    batch_idx=batch_idx,
                    scaler=None,  # No scaler used in classification training
                    grad_clip_threshold=self.grad_clip_norm,
                    was_clipped=grad_norm_value > self.grad_clip_norm,
                    preclip_grad_norm=grad_norm_value,
                    postclip_grad_norm=grad_norm_value  # Same as preclip since we clipped already
                )
            
            # Additional classification-specific monitoring (every 500 batches)
            if batch_idx % 500 == 0:
                # Calculate probabilities and confidence metrics
                probs = torch.softmax(logits, dim=-1)
                max_probs, _ = torch.max(probs, dim=-1)
                
                # Log classification-specific metrics
                self.writer.add_scalar("Classification/pred_class_min", predictions.min().item(), global_step)
                self.writer.add_scalar("Classification/pred_class_max", predictions.max().item(), global_step)
                self.writer.add_scalar("Classification/label_class_min", labels.min().item(), global_step)
                self.writer.add_scalar("Classification/label_class_max", labels.max().item(), global_step)
                self.writer.add_scalar("Classification/unique_labels_in_batch", len(torch.unique(labels)), global_step)
                
                # Prediction confidence and uncertainty metrics
                self.writer.add_scalar("Classification/pred_confidence_mean", max_probs.mean().item(), global_step)
                self.writer.add_scalar("Classification/pred_confidence_std", max_probs.std().item(), global_step)
                self.writer.add_scalar("Classification/pred_entropy", -(probs * torch.log_softmax(logits, dim=-1)).sum(dim=-1).mean().item(), global_step)
                
                # Class distribution monitoring
                for class_idx in range(self.num_classes):
                    class_prob_mean = probs[:, class_idx].mean().item()
                    self.writer.add_scalar(f"ClassDist/class_{class_idx}_prob_mean", class_prob_mean, global_step)
                
                # Label distribution in batch
                for class_idx in range(self.num_classes):
                    class_count = (labels == class_idx).sum().item()
                    class_ratio = class_count / labels.size(0)
                    self.writer.add_scalar(f"LabelDist/class_{class_idx}_ratio", class_ratio, global_step)
                
                # Logit statistics for debugging
                self.writer.add_scalar("Logits/mean", logits.mean().item(), global_step)
                self.writer.add_scalar("Logits/std", logits.std().item(), global_step)
                self.writer.add_scalar("Logits/max", logits.max().item(), global_step)
                self.writer.add_scalar("Logits/min", logits.min().item(), global_step)
            
            # Update progress bar
            pbar.set_postfix(
                loss=loss.item(),
                acc=batch_metrics['accuracy'],
                f1=batch_metrics['f1_score'],
                p_ic=batch_metrics['pearson_ic'],
                s_ic=batch_metrics['spearman_ic'],
                grad_norm=grad_norm_value
            )

            # Keyboard stop check
            if self._stop_training_requested:
                early_stop_triggered = True
                print("\n[Keyboard] Early stop triggered. Finishing current epoch summary...")
                break
        
        # Calculate epoch averages
        avg_loss = float(np.mean(epoch_losses))
        avg_accuracy = float(np.mean(epoch_accuracies))
        avg_f1_score = float(np.mean(epoch_f1_scores))
        avg_grad_norm = float(np.mean(epoch_grad_norms))
        
        # Calculate epoch-level metrics with IC
        all_logits_tensor = torch.cat(all_logits, dim=0) if all_logits else None
        all_original_returns_tensor = torch.cat(all_original_returns, dim=0) if all_original_returns else None
        epoch_metrics = calculate_classification_metrics(
            torch.tensor(all_predictions), torch.tensor(all_labels), self.num_classes, 
            all_logits_tensor, all_original_returns_tensor, self.use_return_based_ic
        )
        epoch_accuracy = epoch_metrics['accuracy']
        
        return avg_loss, avg_accuracy, avg_f1_score, avg_grad_norm, epoch_accuracy, epoch_metrics['pearson_ic'], epoch_metrics['spearman_ic']
    
    def validate_epoch(self) -> Tuple[float, float, float, float, float, float]:
        """Validate for one epoch."""
        self.model.eval()
        
        # Validation statistics
        epoch_losses = []
        all_predictions = []
        all_labels = []
        all_logits = []  # For IC calculation
        all_original_returns = []  # For return-based IC calculation
        
        pbar = tqdm(self.valid_loader, desc='Valid', leave=False)
        with torch.no_grad():
            for batch_idx, batch in enumerate(pbar):
                # Extract features, labels, and optional original returns
                if len(batch) == 5:  # With metadata and original returns
                    features, labels, original_returns, _, _ = batch
                elif len(batch) == 4:  # With metadata
                    features, labels, _, _ = batch
                    original_returns = None
                elif len(batch) == 3:  # Without metadata but with original returns
                    features, labels, original_returns = batch
                else:  # Without metadata and original returns
                    features, labels = batch
                    original_returns = None
                
                # Move to device and prepare
                features = features.to(self.device)
                labels = labels.to(self.device).long()
                if original_returns is not None:
                    original_returns = original_returns.to(self.device)
                
                # Forward pass
                logits, fv = self.model(features)
                
                # Calculate loss
                loss = self.loss_fn(logits, labels)
                
                # Calculate predictions and metrics
                predictions = torch.argmax(logits, dim=-1)
                batch_metrics = calculate_classification_metrics(
                    predictions, labels, self.num_classes, logits, 
                    original_returns, self.use_return_based_ic
                )
                
                # Accumulate statistics
                epoch_losses.append(loss.item())
                all_predictions.extend(predictions.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())
                all_logits.append(logits.detach().cpu())  # Store logits for IC
                if original_returns is not None:
                    all_original_returns.append(original_returns.detach().cpu())  # Store original returns for IC
                
                # Update progress bar
                pbar.set_postfix(
                    loss=loss.item(),
                    acc=batch_metrics['accuracy'],
                    p_ic=batch_metrics['pearson_ic'],
                    s_ic=batch_metrics['spearman_ic']
                )
        
        # Calculate epoch metrics with IC
        avg_loss = float(np.mean(epoch_losses))
        all_logits_tensor = torch.cat(all_logits, dim=0) if all_logits else None
        all_original_returns_tensor = torch.cat(all_original_returns, dim=0) if all_original_returns else None
        epoch_metrics = calculate_classification_metrics(
            torch.tensor(all_predictions), torch.tensor(all_labels), self.num_classes, 
            all_logits_tensor, all_original_returns_tensor, self.use_return_based_ic
        )
        
        return avg_loss, epoch_metrics['accuracy'], epoch_metrics['f1_score'], epoch_metrics['precision'], epoch_metrics['pearson_ic'], epoch_metrics['spearman_ic']
    
    def test_model(self, checkpoint_path: Optional[str] = None) -> Tuple[float, float, float, float, float, float]:
        """Test the model on test set."""
        # Re-verify device availability and force GPU detection
        current_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if current_device != self.device:
            print(f"⚠️  Device changed from {self.device} to {current_device}, updating...")
            self.device = current_device
        
        print(f"🔧 Test mode device verification: {self.device}")
        print(f"   CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"   CUDA device count: {torch.cuda.device_count()}")
            print(f"   Current CUDA device: {torch.cuda.current_device()}")
        
        # Load checkpoint if provided
        if checkpoint_path:
            if not os.path.exists(checkpoint_path):
                raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")
            
            print(f"🔧 Loading checkpoint for testing: {checkpoint_path}")
            checkpoint = torch.load(checkpoint_path, map_location=self.device, weights_only=False)
            self.model.load_state_dict(checkpoint["model_state_dict"])
            # Force move to device and verify
            self.model.to(self.device)
            
            # Verify model is actually on the correct device
            model_device = next(self.model.parameters()).device
            print(f"🔍 Model device after loading: {model_device}")
            if model_device != self.device:
                print(f"⚠️  Model device mismatch! Forcing move to {self.device}...")
                self.model = self.model.to(self.device)
                model_device = next(self.model.parameters()).device
                print(f"🔍 Model device after forced move: {model_device}")
            
            epoch = checkpoint.get('epoch', 'N/A')
            val_acc = checkpoint.get('val_accuracy', 'N/A')
            print(f"✅ Checkpoint loaded - Epoch: {epoch}, Val Accuracy: {val_acc}")
        else:
            # Use best model from current training
            best_ckpt_path = self.ckpt_dir / "best_model.pth"
            if best_ckpt_path.exists():
                print(f"🔧 Loading best model for testing: {best_ckpt_path}")
                checkpoint = torch.load(best_ckpt_path, map_location=self.device, weights_only=False)
                self.model.load_state_dict(checkpoint["model_state_dict"])
                # Force move to device and verify
                self.model.to(self.device)
                
                # Verify model is actually on the correct device
                model_device = next(self.model.parameters()).device
                print(f"🔍 Model device after loading: {model_device}")
                if model_device != self.device:
                    print(f"⚠️  Model device mismatch! Forcing move to {self.device}...")
                    self.model = self.model.to(self.device)
                    model_device = next(self.model.parameters()).device
                    print(f"🔍 Model device after forced move: {model_device}")
                
                epoch = checkpoint.get('epoch', 'N/A')
                best_val_acc = checkpoint.get('best_val_acc', 'N/A')
                print(f"✅ Best model loaded - Epoch: {epoch}, Best Val Accuracy: {best_val_acc}")
            else:
                print("⚠️  No best model found, using current model state")
                # Even if no checkpoint, verify model device
                model_device = next(self.model.parameters()).device
                print(f"🔍 Current model device: {model_device}")
                if model_device != self.device:
                    print(f"⚠️  Model device mismatch! Forcing move to {self.device}...")
                    self.model = self.model.to(self.device)
        
        # Ensure loss function is on correct device (recreate if needed)
        self.loss_fn = self._create_loss_function()
        
        # Test evaluation
        self.model.eval()
        
        # Test statistics
        test_losses = []
        all_predictions = []
        all_labels = []
        all_logits = []  # For IC calculation
        all_original_returns = []  # For return-based IC calculation
        
        print("🧪 Testing model on test set...")
        pbar = tqdm(self.test_loader, desc='Test', leave=False)
        with torch.no_grad():
            for batch_idx, batch in enumerate(pbar):
                # Extract features, labels, and optional original returns
                if len(batch) == 5:  # With metadata and original returns
                    features, labels, original_returns, _, _ = batch
                elif len(batch) == 4:  # With metadata
                    features, labels, _, _ = batch
                    original_returns = None
                elif len(batch) == 3:  # Without metadata but with original returns
                    features, labels, original_returns = batch
                else:  # Without metadata and original returns
                    features, labels = batch
                    original_returns = None
                
                # Move to device and prepare
                features = features.to(self.device)
                labels = labels.to(self.device).long()
                if original_returns is not None:
                    original_returns = original_returns.to(self.device)
                
                # Debug: Verify data and model are on same device (first batch only)
                if batch_idx == 0:
                    model_device = next(self.model.parameters()).device
                    features_device = features.device
                    print(f"🔍 First batch device check - Model: {model_device}, Features: {features_device}")
                    if model_device != features_device:
                        print(f"❌ Device mismatch detected! This will cause GPU acceleration issues.")
                        # Force everything to the same device
                        self.model = self.model.to(features_device)
                        self.device = features_device
                        print(f"🔧 Fixed device mismatch, both now on: {self.device}")
                
                # Forward pass
                logits, fv = self.model(features)
                
                # Calculate loss
                loss = self.loss_fn(logits, labels)
                
                # Calculate predictions
                predictions = torch.argmax(logits, dim=-1)
                
                # Accumulate statistics
                test_losses.append(loss.item())
                all_predictions.extend(predictions.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())
                all_logits.append(logits.detach().cpu())  # Store logits for IC
                if original_returns is not None:
                    all_original_returns.append(original_returns.detach().cpu())  # Store original returns for IC
                
                # Update progress bar
                pbar.set_postfix(loss=loss.item())
        
        # Calculate test metrics with IC
        test_loss = float(np.mean(test_losses))
        all_logits_tensor = torch.cat(all_logits, dim=0) if all_logits else None
        all_original_returns_tensor = torch.cat(all_original_returns, dim=0) if all_original_returns else None
        test_metrics = calculate_classification_metrics(
            torch.tensor(all_predictions), torch.tensor(all_labels), self.num_classes, 
            all_logits_tensor, all_original_returns_tensor, self.use_return_based_ic
        )
        
        # Log test results to TensorBoard
        if self.writer is not None:
            epoch_for_logging = self.current_epoch if self.current_epoch > 0 else 1
            self.writer.add_scalar("Test/loss", test_loss, epoch_for_logging)
            self.writer.add_scalar("Test/accuracy", test_metrics['accuracy'], epoch_for_logging)
            self.writer.add_scalar("Test/f1_score", test_metrics['f1_score'], epoch_for_logging)
            self.writer.add_scalar("Test/precision", test_metrics['precision'], epoch_for_logging)
            self.writer.add_scalar("Test/recall", test_metrics['recall'], epoch_for_logging)
            self.writer.add_scalar("Test/pearson_ic", test_metrics['pearson_ic'], epoch_for_logging)
            self.writer.add_scalar("Test/spearman_ic", test_metrics['spearman_ic'], epoch_for_logging)
        
        # Print detailed classification report
        print(f"\n📊 Test Results:")
        print(f"   Test Loss: {test_loss:.6f}")
        print(f"   Test Accuracy: {test_metrics['accuracy']:.6f}")
        print(f"   Test F1 Score: {test_metrics['f1_score']:.6f}")
        print(f"   Test Precision: {test_metrics['precision']:.6f}")
        print(f"   Test Recall: {test_metrics['recall']:.6f}")
        print(f"   Test Pearson IC: {test_metrics['pearson_ic']:.6f}")
        print(f"   Test Spearman IC: {test_metrics['spearman_ic']:.6f}")
        
        # Print detailed classification report
        try:
            from sklearn.metrics import classification_report
            report = classification_report(all_labels, all_predictions, zero_division=0)
            print(f"\n📋 Detailed Classification Report:")
            print(report)
        except Exception as e:
            print(f"Could not generate detailed classification report: {e}")
        
        return test_loss, test_metrics['accuracy'], test_metrics['f1_score'], test_metrics['precision'], test_metrics['pearson_ic'], test_metrics['spearman_ic']
    
    def save_checkpoint(self, val_loss: float, val_accuracy: float, is_best: bool = False):
        """Save model checkpoint."""
        checkpoint = {
            'epoch': self.current_epoch,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict() if self.scheduler else None,
            'val_loss': val_loss,
            'val_accuracy': val_accuracy,
            'best_val_acc': self.best_val_acc,
            'arch_config': self.arch_config,
            'train_config': self.train_config,
            'preset_name': self.preset_name
        }
        
        # Regular checkpoint
        if self.current_epoch % self.save_config.get('save_frequency', 10) == 0:
            checkpoint_path = self.ckpt_dir / f"checkpoint_epoch_{self.current_epoch}.pth"
            torch.save(checkpoint, checkpoint_path)
            
        # Best checkpoint
        if is_best:
            best_path = self.ckpt_dir / "best_model.pth"
            torch.save(checkpoint, best_path)
            print(f"💾 Best model saved: {best_path}")
        
        # Cleanup old checkpoints
        if self.current_epoch % self.save_config.get('save_frequency', 10) == 0:
            self._cleanup_old_checkpoints(self.ckpt_dir)
    
    def _cleanup_old_checkpoints(self, save_dir: Path):
        """Clean up old checkpoints to save space."""
        max_saved = self.save_config.get('max_saved_models', 5)
        
        # Get all checkpoint files
        checkpoint_files = list(save_dir.glob("checkpoint_epoch_*.pth"))
        
        if len(checkpoint_files) > max_saved:
            # Sort by creation time and remove oldest
            checkpoint_files.sort(key=lambda x: x.stat().st_ctime)
            for old_file in checkpoint_files[:-max_saved]:
                old_file.unlink()
                print(f"🗑️  Removed old checkpoint: {old_file}")
    
    def train(self):
        """Main training loop."""
        epochs = self.train_config['epochs']
        patience = self.train_config['early_stopping']['patience']
        
        print(f"\n🚀 Starting classification training for {epochs} epochs...")
        print(f"   Number of classes: {self.num_classes}")
        print(f"   Loss function: {self._get_loss_function_name()}")
        if self.noise_enabled:
            print(f"   Training noise: Gaussian std={self.noise_std}")
        else:
            print(f"   Training noise: Disabled")
        
        for epoch in range(epochs):
            self.current_epoch = epoch + 1
                
            # Train
            print(f"\n📚 Epoch {self.current_epoch}/{epochs}")
            (train_loss, train_avg_acc, train_f1, train_grad_norm, train_epoch_acc, train_pearson_ic, train_spearman_ic) = self.train_epoch()
                
            # Validate
            (val_loss, val_accuracy, val_f1, val_precision, val_pearson_ic, val_spearman_ic) = self.validate_epoch()
                
            # Update learning rate scheduler
            if self.scheduler is not None:
                if isinstance(self.scheduler, optim.lr_scheduler.ReduceLROnPlateau):
                    self.scheduler.step(val_accuracy)  # Use accuracy for classification
                else:
                    self.scheduler.step()
                
            # Log metrics to TensorBoard
            self.writer.add_scalar('Loss/train', train_loss, epoch)
            self.writer.add_scalar('Loss/val', val_loss, epoch)
            self.writer.add_scalar('Accuracy/train', train_epoch_acc, epoch)
            self.writer.add_scalar('Accuracy/val', val_accuracy, epoch)
            self.writer.add_scalar('F1_Score/train', train_f1, epoch)
            self.writer.add_scalar('F1_Score/val', val_f1, epoch)
            self.writer.add_scalar('Precision/val', val_precision, epoch)
            self.writer.add_scalar('IC/train_pearson', train_pearson_ic, epoch)
            self.writer.add_scalar('IC/train_spearman', train_spearman_ic, epoch)
            self.writer.add_scalar('IC/val_pearson', val_pearson_ic, epoch)
            self.writer.add_scalar('IC/val_spearman', val_spearman_ic, epoch)
            self.writer.add_scalar('Training/grad_norm', train_grad_norm, epoch)
            self.writer.add_scalar('Training/lr', self.optimizer.param_groups[0]['lr'], epoch)
            
            # 🚀 Enhanced monitoring: Log epoch-level improvement tracking
            if epoch > 1:  # Only after first epoch
                acc_improvement = val_accuracy - getattr(self, '_prev_val_acc', val_accuracy)
                f1_improvement = val_f1 - getattr(self, '_prev_val_f1', val_f1)
                ic_improvement = val_pearson_ic - getattr(self, '_prev_val_pearson_ic', val_pearson_ic)
                
                self.writer.add_scalar('Improvement/accuracy_delta', acc_improvement, epoch)
                self.writer.add_scalar('Improvement/f1_delta', f1_improvement, epoch)
                self.writer.add_scalar('Improvement/pearson_ic_delta', ic_improvement, epoch)
            
            # Store for next epoch comparison
            self._prev_val_acc = val_accuracy
            self._prev_val_f1 = val_f1
            self._prev_val_pearson_ic = val_pearson_ic
                
            # Print epoch summary
            print(f"📊 Epoch {self.current_epoch} Results:")
            print(f"   Train Loss: {train_loss:.6f}, Train Accuracy: {train_epoch_acc:.4f}")
            print(f"   Train IC: Pearson={train_pearson_ic:.4f}, Spearman={train_spearman_ic:.4f}")
            print(f"   Val Loss: {val_loss:.6f}, Val Accuracy: {val_accuracy:.4f}")
            print(f"   Val F1: {val_f1:.4f}, Val Precision: {val_precision:.4f}")
            print(f"   Val IC: Pearson={val_pearson_ic:.4f}, Spearman={val_spearman_ic:.4f}")
            print(f"   Grad Norm: {train_grad_norm:.4f}, LR: {self.optimizer.param_groups[0]['lr']:.2e}")
                
            # Early stopping and checkpointing based on highest accuracy
            is_best = val_accuracy > self.best_val_acc
            if is_best:
                self.best_val_acc = val_accuracy
                self.patience_counter = 0
            else:
                self.patience_counter += 1
                
            # Save checkpoint
            self.save_checkpoint(val_loss, val_accuracy, is_best)
                
            # Keyboard stop (takes priority)
            if self._stop_training_requested:
                print(f"⏹️  Manual stop requested at epoch {self.current_epoch}")
                print(f"   Best validation accuracy so far: {self.best_val_acc:.6f}")
                break

            # Early stopping
            if self.patience_counter >= patience:
                print(f"⏹️  Early stopping triggered after {epoch + 1} epochs")
                print(f"   Best validation accuracy: {self.best_val_acc:.6f}")
                break
        
        print(f"\n✅ Training completed!")
        print(f"   Final validation loss: {val_loss:.6f}")
        print(f"   Best validation accuracy: {self.best_val_acc:.6f}")
        print(f"   Model saved in: {self.ckpt_dir}")
        
        # Post-training testing logic
        if self._stop_training_requested and self._test_on_stop_requested:
            print(f"\n🧪 Manual test requested. Testing best model on test set...")
            self.test_model()
        else:
            print(f"\n🧪 Testing best model on test set...")
            self.test_model()
        
        # Close writer
        if self.writer is not None:
            self.writer.close()


def main():
    """Main function to run training."""
    import argparse
    import multiprocessing
    
    multiprocessing.set_start_method('spawn', force=True)
    
    # Default checkpoint path
    DEFAULT_CHECKPOINT = "outputs/encoder_only_transformer_classification_TorchTransformerClassification_custom_20250708_010731/ckpt/best_model.pth"
    
    parser = argparse.ArgumentParser(description='Train PyTorch nn.Transformer based encoder-only classification model')
    parser.add_argument('--config', type=str, default='configs/models/transformer/encoder_only_classification.yaml',
                        help='Path to configuration file')
    parser.add_argument('--preset', type=str, default=None,
                        help='Override preset in config file')
    parser.add_argument('--test-only', action='store_true', default=False,
                        help='Only run testing on specified checkpoint')
    parser.add_argument('--checkpoint', type=str, default=DEFAULT_CHECKPOINT,
                        help=f'Path to checkpoint file for testing (default: {DEFAULT_CHECKPOINT})')
    parser.add_argument('--train', action='store_true', default=False,
                        help='Run full training instead of test-only mode')
    parser.add_argument('--debug-gpu', action='store_true', default=False,
                        help='Enable verbose GPU debugging information')
    
    # Feature selection parameters
    parser.add_argument('--selected-factors', type=str, nargs='*', default=None,
                       help="Selected factors list, space separated. E.g.: --selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
    parser.add_argument('--list-factors', action='store_true',
                       help="List all available factor names in the dataset")
    
    # DuckDB performance tuning parameters
    parser.add_argument('--duck-threads', type=int, default=16, help="DuckDB worker threads.")
    parser.add_argument('--duck-memory', type=str, default='16GB', help="DuckDB memory limit.")
    parser.add_argument('--duck-cache', type=str, default='4GB', help="DuckDB object cache size.")
    parser.add_argument('--prefetch-factor', type=int, default=4, help="DataLoader prefetch factor per worker")
    
    args = parser.parse_args()
    
    # Check if config file exists
    if not os.path.exists(args.config):
        print(f"❌ Configuration file not found: {args.config}")
        return
    
    # Handle factor list query
    if args.list_factors:
        try:
            import json
            from pathlib import Path
            dataset_path = "data/Dataset/pv_v5_pv_v4_price&trade_pt10818"
            schema_path = Path(dataset_path) / "meta" / "schema.json"
            if schema_path.exists():
                with schema_path.open("r", encoding="utf-8") as fp:
                    schema_json = json.load(fp)
                expanded_factor_names = schema_json.get("expanded_factor_names", [])
                print("📊 Available factors in dataset:")
                for i, factor in enumerate(expanded_factor_names, 1):
                    print(f"  {i:2d}. {factor}")
                print(f"\nTotal: {len(expanded_factor_names)} factors")
                print("\n💡 Usage example:")
                print("  python train_torch_transformer_classification.py --selected-factors adj_close_mar_w1 adj_open_mar_w1 vwap_mar_w1")
            else:
                print(f"❌ Dataset schema file not found: {schema_path}")
        except Exception as e:
            print(f"❌ Failed to get factor list: {e}")
        return
    
    # Handle mutual exclusion of test-only and train modes
    if args.train:
        args.test_only = False
    elif not args.test_only and not args.train:
        # Default behavior: run training if neither flag is specified
        args.test_only = False
    
    # Validate test-only mode arguments
    if args.test_only:
        if args.checkpoint is None:
            print(f"❌ --checkpoint is required when using --test-only mode")
            return
        if not os.path.exists(args.checkpoint):
            print(f"❌ Checkpoint file not found: {args.checkpoint}")
            print(f"💡 To run training instead, use: python train_torch_transformer_classification.py --train")
            return
    
    # Override preset if specified
    if args.preset:
        print(f"🔧 Overriding preset to: {args.preset}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        config['preset'] = args.preset
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # Handle selected factors parameter
    if args.selected_factors:
        print(f"🔧 Overriding selected factors: {args.selected_factors}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        if 'data' not in config:
            config['data'] = {}
        config['data']['selected_factors'] = args.selected_factors
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # Handle DuckDB performance tuning parameters
    if args.duck_threads or args.duck_memory or args.duck_cache or args.prefetch_factor:
        print(f"🔧 Overriding DuckDB settings: threads={args.duck_threads}, memory={args.duck_memory}, cache={args.duck_cache}, prefetch={args.prefetch_factor}")
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        if 'data' not in config:
            config['data'] = {}
        if args.duck_threads:
            config['data']['duck_threads'] = args.duck_threads
        if args.duck_memory:
            config['data']['duck_memory'] = args.duck_memory
        if args.duck_cache:
            config['data']['duck_cache'] = args.duck_cache
        if args.prefetch_factor:
            config['data']['prefetch_factor'] = args.prefetch_factor
        with open(args.config, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    # Create trainer
    trainer = TorchTransformerClassificationTrainer(args.config)
    
    # Enable GPU debugging if requested
    if args.debug_gpu:
        print(f"\n🔍 GPU Debug Mode Enabled")
        trainer._verify_device_setup()  # Force device verification
    
    if args.test_only:
        # Run test-only mode
        print(f"🧪 Running test-only mode with checkpoint: {args.checkpoint}")
        trainer.test_model(args.checkpoint)
    else:
        # Run full training
        trainer.train()


if __name__ == "__main__":
    main()

