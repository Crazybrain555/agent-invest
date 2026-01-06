#!/usr/bin/env python3
"""
Model Prediction Runner

This script loads a trained ordinal classification model and runs predictions
on all samples from train, validation, and test datasets. Results are saved
to Excel with comprehensive metadata including dates and stock codes.
"""

import os
import sys
import yaml
import torch
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from tqdm import tqdm
import warnings
import multiprocessing
from datetime import datetime
warnings.filterwarnings('ignore')

# Add the src directory to the Python path
sys.path.append(str(Path(__file__).parent / "src"))

from src.models.transformer.torch.torch_classification_transformer import TorchTransformerClassifier
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders


class ModelPredictionRunner:
    """Runner for generating comprehensive model predictions."""
    
    def __init__(self, config_path: str, checkpoint_path: str):
        self.config_path = config_path
        self.checkpoint_path = checkpoint_path
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load configuration
        self.config = self._load_config()
        self.arch_config = self.config.get('architecture', {})
        self.data_config = self.config.get('data', {})
        self.train_config = self.config.get('training', {})
        
        self.num_classes = self.arch_config['num_classes']
        
        # Load model
        self.model = self._load_model()
        
        # Create dataloaders with metadata
        self.train_loader, self.valid_loader, self.test_loader = self._create_dataloaders()
        
        print(f"✅ Model prediction runner initialized successfully!")
        print(f"   Device: {self.device}")
        print(f"   Number of classes: {self.num_classes}")
        print(f"   Train batches: {len(self.train_loader)}")
        print(f"   Valid batches: {len(self.valid_loader)}")
        print(f"   Test batches: {len(self.test_loader)}")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config
    
    def _resolve_preset_architecture(self) -> Dict[str, Any]:
        """Resolve architecture config from preset."""
        preset = self.config.get('preset', 'custom')
        presets = self.config.get('presets', {})
        
        if preset in presets and 'architecture' in presets[preset]:
            print(f"✅ Using architecture from preset '{preset}'")
            return presets[preset]['architecture']
        else:
            print(f"⚠️  Preset '{preset}' not found or no architecture, using top-level architecture")
            return self.config.get('architecture', {})
    
    def _load_model(self) -> TorchTransformerClassifier:
        """Load the trained model from checkpoint."""
        if not os.path.exists(self.checkpoint_path):
            raise FileNotFoundError(f"Checkpoint not found: {self.checkpoint_path}")
        
        print(f"🔧 Loading model from checkpoint: {self.checkpoint_path}")
        checkpoint = torch.load(self.checkpoint_path, map_location=self.device, weights_only=False)
        
        # Use architecture config from checkpoint if available (actual training config)
        if 'arch_config' in checkpoint:
            arch_config = checkpoint['arch_config']
            print("✅ Using architecture config from checkpoint (actual training config)")
            # Update instance variables to use checkpoint architecture
            self.arch_config = arch_config
            self.num_classes = arch_config['num_classes']
        else:
            # Fallback: resolve preset from config file
            print("⚠️  No arch_config in checkpoint, resolving from preset...")
            arch_config = self._resolve_preset_architecture()
        
        print(f"📊 Model architecture (from checkpoint):")
        print(f"   - d_model: {arch_config.get('d_model')}")
        print(f"   - num_encoder_layers: {arch_config.get('num_encoder_layers')}")
        print(f"   - input_size: {arch_config.get('input_size')}")
        print(f"   - num_classes: {arch_config.get('num_classes')}")
        
        # Create model
        model = TorchTransformerClassifier(
            input_size=arch_config['input_size'],
            seq_length=arch_config['seq_length'],
            d_model=arch_config['d_model'],
            nhead=arch_config['nhead'],
            num_encoder_layers=arch_config['num_encoder_layers'],
            dim_feedforward=arch_config['dim_feedforward'],
            num_classes=arch_config['num_classes'],
            dropout=arch_config['dropout'],
            activation=arch_config['activation'],
            positional_encoding=arch_config['positional_encoding'],
            embedding_type=arch_config['embedding_type'],
            norm_type=arch_config.get('norm_type', 'layer'),
            norm_first=arch_config.get('norm_first', True),
            pooling=arch_config.get('pooling', 'mean'),
            feature_dim=arch_config.get('feature_dim', None)
        )
        
        # Load state dict
        model.load_state_dict(checkpoint["model_state_dict"])
        model.to(self.device)
        model.eval()
        
        epoch = checkpoint.get('epoch', 'N/A')
        val_acc = checkpoint.get('val_accuracy', 'N/A')
        print(f"✅ Model loaded - Epoch: {epoch}, Val Accuracy: {val_acc}")
        
        return model
    
    def _create_dataloaders(self):
        """Create train, validation, and test dataloaders with metadata."""
        # Get classification label name
        classification_label_name = self.data_config.get('classification_label_name', None)
        if not classification_label_name:
            classification_label_name = f'classification_label_{self.num_classes}'
        
        # Prepare dataloader config
        dataloader_config = {
            "dataset_path": self.data_config['dataset_path'],
            "batch_size": 32,  # Use smaller batch size for inference
            "num_workers": 4,
            "seed": 42,
            "chunk_size": self.data_config['chunk_size'],
            "memory_limit": self.data_config['memory_limit'],
            "use_fixed_indices": self.data_config['use_fixed_indices'],
            "reverse_seq": self.data_config['reverse_seq'],
            "use_custom_splits": self.data_config.get('use_custom_splits', False),
            "date_ranges": self.data_config.get('date_ranges', None),
            "selected_factors": self.data_config.get('selected_factors', None),
            "duck_threads": self.data_config.get('duck_threads', 16),
            "duck_memory": self.data_config.get('duck_memory', '16GB'),
            "duck_cache": self.data_config.get('duck_cache', '4GB'),
            "prefetch_factor": self.data_config.get('prefetch_factor', 4)
        }
        
        # Create dataloaders with metadata enabled
        train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
            config=dataloader_config,
            keep_meta_train=True,  # Enable metadata for train
            keep_meta_eval=True,   # Enable metadata for validation and test
            use_fixed_indices=dataloader_config['use_fixed_indices'],
            selected_factors=dataloader_config['selected_factors'],
            label_type='classification',
            classification_label_name=classification_label_name
        )
        
        return train_loader, valid_loader, test_loader
    
    def predict_dataset(self, dataloader, split_name: str) -> pd.DataFrame:
        """
        Run predictions on a dataset and return results as DataFrame.
        
        Args:
            dataloader: The dataloader for the dataset
            split_name: Name of the split ('train', 'valid', 'test')
            
        Returns:
            DataFrame with predictions and metadata
        """
        print(f"📊 Running predictions on {split_name} dataset...")
        
        results = []
        
        with torch.no_grad():
            for batch_idx, batch in enumerate(tqdm(dataloader, desc=f'Predicting {split_name}')):
                # Extract features, labels, dates, and stock codes
                if len(batch) == 4:  # With metadata
                    features, labels, dates, stock_codes = batch
                else:  # Without metadata (shouldn't happen with keep_meta=True)
                    features, labels = batch
                    dates = [f"unknown_date_{i}" for i in range(len(labels))]
                    stock_codes = [f"unknown_stock_{i}" for i in range(len(labels))]
                
                # Move to device
                features = features.to(self.device)
                labels = labels.to(self.device).long()
                
                # Forward pass
                logits, _ = self.model(features)
                probabilities = torch.softmax(logits, dim=-1)
                predicted_classes = torch.argmax(logits, dim=-1)
                
                # Convert to numpy
                logits_np = logits.cpu().numpy()
                probabilities_np = probabilities.cpu().numpy()
                predicted_classes_np = predicted_classes.cpu().numpy()
                labels_np = labels.cpu().numpy()
                
                # Create batch results
                for i in range(len(labels)):
                    sample_result = {
                        'split': split_name,
                        'batch_idx': batch_idx,
                        'sample_idx': i,
                        'date': dates[i],
                        'stock_code': stock_codes[i],
                        'true_label': labels_np[i],
                        'predicted_label': predicted_classes_np[i],
                        'prediction_correct': labels_np[i] == predicted_classes_np[i],
                        'max_probability': np.max(probabilities_np[i]),
                        'prediction_confidence': np.max(probabilities_np[i]),
                        # Add individual class probabilities
                        **{f'prob_class_{j}': probabilities_np[i, j] for j in range(self.num_classes)},
                        # Add individual logits
                        **{f'logit_class_{j}': logits_np[i, j] for j in range(self.num_classes)},
                    }
                    
                    # Calculate ordinal metrics
                    ordinal_error = predicted_classes_np[i] - labels_np[i]
                    sample_result['ordinal_error'] = ordinal_error
                    sample_result['abs_ordinal_error'] = abs(ordinal_error)
                    
                    # Calculate expected class value (weighted average)
                    class_values = np.arange(self.num_classes)
                    expected_class = np.sum(probabilities_np[i] * class_values)
                    sample_result['expected_class_value'] = expected_class
                    
                    results.append(sample_result)
        
        df = pd.DataFrame(results)
        print(f"✅ Completed {split_name} predictions: {len(df)} samples")
        
        # Print summary statistics
        accuracy = df['prediction_correct'].mean()
        mae = df['abs_ordinal_error'].mean()
        within_1 = (df['abs_ordinal_error'] <= 1).mean()
        
        print(f"📊 {split_name.upper()} Summary:")
        print(f"   - Accuracy: {accuracy:.4f}")
        print(f"   - Mean Absolute Error: {mae:.4f}")
        print(f"   - Within 1 class: {within_1:.4f}")
        
        return df
    
    def process_for_portfolio_optimization(self, 
                                         df: pd.DataFrame,
                                         top_n_per_class: Optional[int] = 50,
                                         rebalance_frequency_days: int = 10,
                                         min_confidence: float = 0.6,
                                         ignore_min_confidence: bool = False,
                                         splits_to_include: List[str] = ['test'],
                                         sort_by_confidence: bool = True,
                                         target_classes: Optional[Any] = None) -> pd.DataFrame:
        """
        Process predictions for portfolio optimization with rebalancing logic.
        
        Args:
            df: DataFrame with all predictions
            top_n_per_class: Number of top predictions to select per class per rebalance date (B)
                           None or -1 = select all predictions (no limit)
            rebalance_frequency_days: Frequency of rebalancing in days (10 days)
            min_confidence: Minimum prediction confidence threshold
            ignore_min_confidence: If True, bypass confidence filtering to select all predictions
            splits_to_include: Which data splits to include ('train', 'valid', 'test')
            sort_by_confidence: Whether to sort by confidence (True) or by expected class value (False)
            target_classes: Classes to include - None=all classes, [0,1,49]=specific classes, (40,49)=range inclusive
            
        Returns:
            Processed DataFrame for portfolio optimization
            Total entries = A target classes × (B predictions OR all predictions) × number of rebalance dates
        """
        print(f"🔄 Processing data for portfolio optimization with rebalancing logic...")
        print(f"   Rebalance frequency: Every {rebalance_frequency_days} days")
        
        # Handle "select all" cases
        select_all_predictions = top_n_per_class is None or top_n_per_class == -1
        if select_all_predictions:
            print(f"   Top N per class per rebalance: ALL (no limit)")
        else:
            print(f"   Top N per class per rebalance: {top_n_per_class}")
        
        if ignore_min_confidence:
            print(f"   Min confidence: IGNORED (all predictions selected)")
        else:
            print(f"   Min confidence: {min_confidence}")
        
        print(f"   Splits included: {splits_to_include}")
        
        # Determine which classes to process
        if target_classes is None:
            # Use all classes
            classes_to_process = list(range(self.num_classes))
            print(f"   Target classes: All classes (0 to {self.num_classes-1})")
        elif isinstance(target_classes, (list, tuple)) and len(target_classes) == 2 and isinstance(target_classes[0], int) and isinstance(target_classes[1], int):
            # Check if it's a range tuple like (40, 49)
            if target_classes[0] <= target_classes[1]:
                classes_to_process = list(range(target_classes[0], target_classes[1] + 1))  # +1 for inclusive range
                print(f"   Target classes: Range {target_classes[0]} to {target_classes[1]} (inclusive)")
            else:
                # Treat as specific classes list
                classes_to_process = list(target_classes)
                print(f"   Target classes: Specific classes {target_classes}")
        elif isinstance(target_classes, (list, tuple)):
            # Specific classes list like [0, 1, 49]
            classes_to_process = list(target_classes)
            print(f"   Target classes: Specific classes {target_classes}")
        elif isinstance(target_classes, int):
            # Single class like 49
            classes_to_process = [target_classes]
            print(f"   Target classes: Single class {target_classes}")
        else:
            # Fallback to all classes
            classes_to_process = list(range(self.num_classes))
            print(f"   Target classes: Invalid format, using all classes (0 to {self.num_classes-1})")
        
        # Validate classes are within valid range
        valid_classes = [c for c in classes_to_process if 0 <= c < self.num_classes]
        if len(valid_classes) != len(classes_to_process):
            invalid_classes = [c for c in classes_to_process if c not in valid_classes]
            print(f"   ⚠️ Warning: Ignoring invalid classes {invalid_classes} (valid range: 0-{self.num_classes-1})")
            classes_to_process = valid_classes
        
        if len(classes_to_process) == 0:
            print("   ❌ No valid classes to process!")
            return pd.DataFrame()
        
        print(f"   Final classes to process: {classes_to_process} (total: {len(classes_to_process)})")
        
        # Filter by splits and optionally by confidence
        portfolio_df = df[df['split'].isin(splits_to_include)].copy()
        
        if not ignore_min_confidence:
            portfolio_df = portfolio_df[portfolio_df['prediction_confidence'] >= min_confidence]
            print(f"   Samples after split/confidence filtering: {len(portfolio_df):,}")
        else:
            print(f"   Samples after split filtering (confidence ignored): {len(portfolio_df):,}")
        
        if len(portfolio_df) == 0:
            print("⚠️  No samples meet the filtering criteria!")
            return pd.DataFrame()
        
        # Convert date to datetime
        portfolio_df['date_dt'] = pd.to_datetime(portfolio_df['date'], format='%Y%m%d', errors='coerce')
        
        # Remove rows with invalid dates
        portfolio_df = portfolio_df.dropna(subset=['date_dt'])
        
        if len(portfolio_df) == 0:
            print("⚠️  No valid dates found!")
            return pd.DataFrame()
        
        # Determine rebalance dates
        min_date = portfolio_df['date_dt'].min()
        max_date = portfolio_df['date_dt'].max()
        
        # Create rebalance dates every N days starting from min_date
        rebalance_dates = []
        current_date = min_date
        while current_date <= max_date:
            rebalance_dates.append(current_date)
            current_date += pd.Timedelta(days=rebalance_frequency_days)
        
        print(f"   Date range: {min_date.strftime('%Y-%m-%d')} to {max_date.strftime('%Y-%m-%d')}")
        print(f"   Total rebalance dates: {len(rebalance_dates)}")
        print(f"   Rebalance dates: {[d.strftime('%Y-%m-%d') for d in rebalance_dates[:5]]}{'...' if len(rebalance_dates) > 5 else ''}")
        
        # Select predictions for each rebalance date
        selected_predictions = []
        
        for rebalance_idx, rebalance_date in enumerate(rebalance_dates):
            # Get predictions for this specific rebalance date
            date_data = portfolio_df[portfolio_df['date_dt'] == rebalance_date]
            
            if len(date_data) == 0:
                # If no predictions for exact date, skip this rebalance date
                continue
            
            print(f"   Processing rebalance date {rebalance_date.strftime('%Y-%m-%d')}: {len(date_data)} predictions available")
            
            # For each target class (A classes), select top B predictions
            for class_idx in classes_to_process:
                # Get predictions for this class on this rebalance date
                class_data = date_data[date_data['predicted_label'] == class_idx]
                
                if len(class_data) == 0:
                    continue
                
                # Sort by confidence or accuracy and select top N (or all)
                if select_all_predictions:
                    # Select all predictions for this class (just sort them)
                    if sort_by_confidence:
                        class_data_sorted = class_data.sort_values('prediction_confidence', ascending=False)
                    else:
                        class_data_sorted = class_data.sort_values('abs_ordinal_error', ascending=True)
                else:
                    # Select top N predictions
                    if sort_by_confidence:
                        class_data_sorted = class_data.nlargest(top_n_per_class, 'prediction_confidence')
                    else:
                        # Sort by accuracy (smallest ordinal error)
                        class_data_sorted = class_data.nsmallest(top_n_per_class, 'abs_ordinal_error')
                
                # Add rebalancing metadata
                class_data_sorted = class_data_sorted.copy()
                class_data_sorted['rebalance_date'] = rebalance_date
                class_data_sorted['rebalance_idx'] = rebalance_idx
                class_data_sorted['days_to_rebalance'] = (class_data_sorted['date_dt'] - rebalance_date).dt.days
                
                selected_predictions.append(class_data_sorted)
        
        if not selected_predictions:
            print("⚠️  No predictions selected for portfolio optimization!")
            print("   This might happen if:")
            print("   - No predictions exist on any rebalance dates")
            print("   - Confidence threshold is too high")
            print("   - No predictions meet the criteria")
            return pd.DataFrame()
        
        # Combine all selected predictions
        portfolio_optimized = pd.concat(selected_predictions, ignore_index=True)
        
        # Add portfolio-specific columns
        portfolio_optimized['signal_strength'] = portfolio_optimized['prediction_confidence']
        
        # Calculate class-specific metrics
        portfolio_optimized['class_prob'] = portfolio_optimized.apply(
            lambda row: row[f'prob_class_{int(row["predicted_label"])}'], axis=1
        )
        
        # Add ranking within rebalance date and class
        portfolio_optimized['rank_in_rebalance_class'] = portfolio_optimized.groupby(['rebalance_date', 'predicted_label'])['prediction_confidence'].rank(method='dense', ascending=False)
        
        # Calculate theoretical maximum entries
        num_rebalance_dates_with_data = portfolio_optimized['rebalance_date'].nunique()
        num_target_classes = len(classes_to_process)
        
        print(f"✅ Portfolio optimization processing complete:")
        print(f"   Selected predictions: {len(portfolio_optimized):,}")
        print(f"   Rebalance dates with data: {num_rebalance_dates_with_data}")
        print(f"   Target classes processed: {num_target_classes} (out of {self.num_classes} total)")
        print(f"   Classes: {classes_to_process}")
        
        if select_all_predictions:
            print(f"   Selection mode: ALL predictions per class (no limit)")
            print(f"   Confidence filtering: {'IGNORED' if ignore_min_confidence else 'APPLIED'}")
        else:
            print(f"   Top N per class: {top_n_per_class}")
            theoretical_max = num_target_classes * top_n_per_class * num_rebalance_dates_with_data
            print(f"   Theoretical max entries: {theoretical_max:,} (A×B×dates = {num_target_classes}×{top_n_per_class}×{num_rebalance_dates_with_data})")
            print(f"   Coverage: {len(portfolio_optimized)/theoretical_max*100 if theoretical_max > 0 else 0:.1f}%")
        
        # Show distribution by rebalance date
        if len(portfolio_optimized) > 0:
            avg_per_date = len(portfolio_optimized) / num_rebalance_dates_with_data
            print(f"   Average predictions per rebalance date: {avg_per_date:.1f}")
        
        return portfolio_optimized
    
    def generate_all_predictions(self, 
                               sample_output_path: str = "model_predictions_sample.xlsx",
                               portfolio_output_path: str = "model_predictions_portfolio.xlsx",
                               sample_fraction: float = 0.001,  # 0.1%
                               portfolio_config: Optional[Dict[str, Any]] = None):
        """
        Generate predictions and save to two Excel files:
        1. Sample file: Random sample of all predictions for inspection
        2. Portfolio file: Processed predictions for portfolio optimization
        
        Args:
            sample_output_path: Path for sample Excel file
            portfolio_output_path: Path for portfolio Excel file  
            sample_fraction: Fraction of data to include in sample file (0.001 = 0.1%)
            portfolio_config: Configuration for portfolio processing
        """
        print(f"🚀 Generating comprehensive model predictions...")
        print(f"   Sample file: {sample_output_path} ({sample_fraction:.1%} of data)")
        print(f"   Portfolio file: {portfolio_output_path}")
        
        # Default portfolio configuration
        default_portfolio_config = {
            'top_n_per_class': 50,
            'rebalance_frequency_days': 10,
            'min_confidence': 0.6,
            'ignore_min_confidence': False,
            'splits_to_include': ['test'],
            'sort_by_confidence': True,
            'target_classes': None
        }
        
        if portfolio_config:
            default_portfolio_config.update(portfolio_config)
        
        portfolio_config = default_portfolio_config
        
        # Generate predictions for each split
        train_df = self.predict_dataset(self.train_loader, 'train')
        valid_df = self.predict_dataset(self.valid_loader, 'valid')
        test_df = self.predict_dataset(self.test_loader, 'test')
        
        # Combine all results
        all_predictions = pd.concat([train_df, valid_df, test_df], ignore_index=True)
        
        # Add overall statistics
        print(f"\n📊 OVERALL STATISTICS:")
        print(f"   Total samples: {len(all_predictions):,}")
        print(f"   Train samples: {len(train_df):,}")
        print(f"   Valid samples: {len(valid_df):,}")
        print(f"   Test samples: {len(test_df):,}")
        
        # Create summary statistics by split
        summary_stats = []
        for split in ['train', 'valid', 'test']:
            split_data = all_predictions[all_predictions['split'] == split]
            stats = {
                'split': split,
                'num_samples': len(split_data),
                'accuracy': split_data['prediction_correct'].mean(),
                'mean_absolute_error': split_data['abs_ordinal_error'].mean(),
                'within_1_class': (split_data['abs_ordinal_error'] <= 1).mean(),
                'mean_confidence': split_data['prediction_confidence'].mean(),
                'std_confidence': split_data['prediction_confidence'].std(),
            }
            
            # Add class-wise accuracy
            for class_idx in range(self.num_classes):
                class_mask = split_data['true_label'] == class_idx
                if class_mask.any():
                    class_accuracy = split_data[class_mask]['prediction_correct'].mean()
                    stats[f'accuracy_class_{class_idx}'] = class_accuracy
                else:
                    stats[f'accuracy_class_{class_idx}'] = np.nan
            
            summary_stats.append(stats)
        
        summary_df = pd.DataFrame(summary_stats)
        
        # Create detailed class distribution
        class_distribution = []
        for split in ['train', 'valid', 'test']:
            split_data = all_predictions[all_predictions['split'] == split]
            for class_idx in range(self.num_classes):
                true_count = (split_data['true_label'] == class_idx).sum()
                pred_count = (split_data['predicted_label'] == class_idx).sum()
                class_distribution.append({
                    'split': split,
                    'class': class_idx,
                    'true_count': true_count,
                    'predicted_count': pred_count,
                    'true_ratio': true_count / len(split_data) if len(split_data) > 0 else 0,
                    'predicted_ratio': pred_count / len(split_data) if len(split_data) > 0 else 0,
                })
        
        class_dist_df = pd.DataFrame(class_distribution)
        
        # Add metadata about the run
        metadata = {
            'run_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'config_path': self.config_path,
            'checkpoint_path': self.checkpoint_path,
            'num_classes': self.num_classes,
            'device': str(self.device),
            'total_samples': len(all_predictions),
            'train_samples': len(train_df),
            'valid_samples': len(valid_df),
            'test_samples': len(test_df),
        }
        
        metadata_df = pd.DataFrame([metadata])
        
        # ===== SAVE SAMPLE FILE (0.1% of data) =====
        sample_size = max(1, int(len(all_predictions) * sample_fraction))
        sample_predictions = all_predictions.sample(n=sample_size, random_state=42).reset_index(drop=True)
        
        print(f"\n💾 Saving sample file to {sample_output_path}...")
        print(f"   Sample size: {len(sample_predictions):,} ({sample_fraction:.1%} of total)")
        if 'stock_code' in sample_predictions.columns:
            print(f"   Stock codes converted to strings for preservation (e.g., '{sample_predictions['stock_code'].iloc[0]}')")
        
        with pd.ExcelWriter(sample_output_path, engine='openpyxl') as writer:
            # Sample predictions with stock_code as string
            sample_predictions_copy = sample_predictions.copy()
            if 'stock_code' in sample_predictions_copy.columns:
                sample_predictions_copy['stock_code'] = sample_predictions_copy['stock_code'].astype(str)
            
            sample_predictions_copy.to_excel(writer, sheet_name='Sample_Predictions', index=False)
            
            # Format the stock_code column in Excel as text  
            if 'stock_code' in sample_predictions_copy.columns:
                sample_worksheet = writer.sheets['Sample_Predictions']
                stock_col_idx = list(sample_predictions_copy.columns).index('stock_code')
                col_letter = chr(65 + stock_col_idx)  # Convert to Excel column letter
                for row in range(2, len(sample_predictions_copy) + 2):
                    cell = sample_worksheet[f'{col_letter}{row}']
                    cell.number_format = '@'  # Text format
            
            # Summary statistics
            summary_df.to_excel(writer, sheet_name='Summary_Statistics', index=False)
            
            # Class distribution
            class_dist_df.to_excel(writer, sheet_name='Class_Distribution', index=False)
            
            # Metadata
            metadata_df.to_excel(writer, sheet_name='Run_Metadata', index=False)
        
        print(f"✅ Sample file saved with {len(sample_predictions):,} samples across 4 sheets")
        
        # ===== SAVE PORTFOLIO FILE (Processed for optimization) =====
        print(f"\n💾 Processing and saving portfolio file to {portfolio_output_path}...")
        
        # Process data for portfolio optimization
        portfolio_predictions = self.process_for_portfolio_optimization(
            all_predictions, **portfolio_config
        )
        
        if len(portfolio_predictions) > 0 and 'stock_code' in portfolio_predictions.columns:
            print(f"   Stock codes will be preserved as strings (e.g., '{portfolio_predictions['stock_code'].iloc[0]}')")
        
        # Create portfolio-specific metadata
        portfolio_metadata = {
            **metadata,
            'portfolio_samples': len(portfolio_predictions),
            'top_n_per_class': portfolio_config['top_n_per_class'],
            'rebalance_frequency_days': portfolio_config['rebalance_frequency_days'],
            'min_confidence': portfolio_config['min_confidence'],
            'ignore_min_confidence': portfolio_config['ignore_min_confidence'],
            'splits_included': str(portfolio_config['splits_to_include']),
            'sort_by_confidence': portfolio_config['sort_by_confidence'],
            'target_classes': str(portfolio_config['target_classes']),
        }
        
        portfolio_metadata_df = pd.DataFrame([portfolio_metadata])
        
        # Create rebalance date summary
        if len(portfolio_predictions) > 0:
            rebalance_summary = portfolio_predictions.groupby(['rebalance_date', 'predicted_label']).agg({
                'prediction_confidence': ['count', 'mean', 'std'],
                'signal_strength': 'mean',
                'class_prob': 'mean'
            }).round(4)
            
            rebalance_summary.columns = ['count', 'mean_confidence', 'std_confidence', 'mean_signal_strength', 'mean_class_prob']
            rebalance_summary = rebalance_summary.reset_index()
        else:
            rebalance_summary = pd.DataFrame()
        
        with pd.ExcelWriter(portfolio_output_path, engine='openpyxl') as writer:
            # Main portfolio predictions
            if len(portfolio_predictions) > 0:
                # Ensure stock_code is string before saving to preserve leading zeros
                portfolio_predictions_copy = portfolio_predictions.copy()
                if 'stock_code' in portfolio_predictions_copy.columns:
                    portfolio_predictions_copy['stock_code'] = portfolio_predictions_copy['stock_code'].astype(str)
                
                portfolio_predictions_copy.to_excel(writer, sheet_name='Portfolio_Predictions', index=False)
                
                # Format the stock_code column in Excel as text
                portfolio_worksheet = writer.sheets['Portfolio_Predictions'] 
                if 'stock_code' in portfolio_predictions_copy.columns:
                    stock_col_idx = list(portfolio_predictions_copy.columns).index('stock_code')
                    col_letter = chr(65 + stock_col_idx)  # Convert to Excel column letter (A=0, B=1, etc.)
                    for row in range(2, len(portfolio_predictions_copy) + 2):
                        cell = portfolio_worksheet[f'{col_letter}{row}']
                        cell.number_format = '@'  # Text format
                
                # Rebalance date summary
                rebalance_summary.to_excel(writer, sheet_name='Rebalance_Summary', index=False)
            else:
                # Create empty sheet with explanation
                empty_df = pd.DataFrame([{'Message': 'No predictions meet the portfolio filtering criteria'}])
                empty_df.to_excel(writer, sheet_name='Portfolio_Predictions', index=False)
            
            # Portfolio configuration
            config_df = pd.DataFrame([portfolio_config])
            config_df.to_excel(writer, sheet_name='Portfolio_Config', index=False)
            
            # Summary statistics
            summary_df.to_excel(writer, sheet_name='Summary_Statistics', index=False)
            
            # Portfolio metadata
            portfolio_metadata_df.to_excel(writer, sheet_name='Run_Metadata', index=False)
        
        if len(portfolio_predictions) > 0:
            print(f"✅ Portfolio file saved with {len(portfolio_predictions):,} optimized predictions across 5 sheets:")
            print(f"   - Portfolio_Predictions: Top predictions for portfolio optimization")
            print(f"   - Rebalance_Summary: Aggregated statistics by rebalance date")
            print(f"   - Portfolio_Config: Configuration parameters used")
            print(f"   - Summary_Statistics: Overall model performance")
            print(f"   - Run_Metadata: Run information and parameters")
        else:
            print("⚠️  Portfolio file saved but contains no predictions (filtering criteria too strict)")
        
        return all_predictions, summary_df, class_dist_df, metadata_df, portfolio_predictions

    def create_trading_format_excel(self, 
                                   portfolio_excel_path: str, 
                                   output_excel_path: str = "trading_positions.xlsx",
                                   target_class: int = 49,
                                   top_n_per_date: Optional[int] = 10,
                                   position_weight_pct: Optional[float] = 10.0,
                                   auto_weight_distribution: bool = True):
        """
        Create a trading-format Excel file from the portfolio predictions.
        
        Args:
            portfolio_excel_path: Path to the portfolio Excel file
            output_excel_path: Path for the trading format Excel file
            target_class: Class to focus on (49 for highest return class)
            top_n_per_date: Number of top predictions per rebalance date (None = all available)
            position_weight_pct: Weight percentage for each position (None = auto-distribute evenly)
            auto_weight_distribution: Whether to automatically distribute weights evenly
        """
        print(f"📈 Creating trading format Excel...")
        print(f"   Input portfolio file: {portfolio_excel_path}")
        print(f"   Output trading file: {output_excel_path}")
        print(f"   Target class: {target_class}")
        
        # Handle "select all" cases for trading
        select_all_trading = top_n_per_date is None or top_n_per_date == -1
        if select_all_trading:
            print(f"   Top N per date: ALL available predictions")
        else:
            print(f"   Top N per date: {top_n_per_date}")
        
        if auto_weight_distribution and position_weight_pct is None:
            print(f"   Position weight: AUTO-DISTRIBUTE (evenly across positions)")
        else:
            print(f"   Position weight: {position_weight_pct}% (fixed)")
        
        try:
            # Read the portfolio predictions with stock_code as string to preserve leading zeros
            portfolio_df = pd.read_excel(
                portfolio_excel_path, 
                sheet_name='Portfolio_Predictions',
                dtype={'stock_code': str}  # Ensure stock codes are read as strings
            )
            
            # Double-check that stock_code is string type and convert if needed
            if 'stock_code' in portfolio_df.columns:
                portfolio_df['stock_code'] = portfolio_df['stock_code'].astype(str)
            
            print(f"   Loaded portfolio data: {len(portfolio_df):,} predictions")
            if len(portfolio_df) > 0:
                sample_stock = portfolio_df['stock_code'].iloc[0]
                print(f"   Stock codes preserved as strings (e.g., '{sample_stock}', type: {type(sample_stock).__name__})")
            
            # Filter for target class only
            class_df = portfolio_df[portfolio_df['predicted_label'] == target_class].copy()
            print(f"   Class {target_class} predictions: {len(class_df):,}")
            
            if len(class_df) == 0:
                print(f"   ❌ No predictions found for class {target_class}!")
                return
            
            # Group by rebalance date and select top N (or all) by confidence
            trading_positions = []
            
            for rebalance_date in class_df['rebalance_date'].unique():
                date_data = class_df[class_df['rebalance_date'] == rebalance_date]
                
                # Sort by prediction confidence and select top N or all
                if select_all_trading:
                    # Select all predictions for this rebalance date
                    top_predictions = date_data.sort_values('prediction_confidence', ascending=False)
                else:
                    # Select top N predictions
                    top_predictions = date_data.nlargest(top_n_per_date, 'prediction_confidence')
                
                # Calculate position weights for this rebalance date
                num_positions = len(top_predictions)
                if num_positions == 0:
                    continue
                
                if auto_weight_distribution and position_weight_pct is None:
                    # Auto-distribute weights evenly (100% / number of positions)
                    individual_weight = 100.0 / num_positions
                else:
                    # Use fixed weight
                    individual_weight = position_weight_pct
                
                # Convert to trading format
                for _, row in top_predictions.iterrows():
                    trading_position = {
                        'Adjusment Date': pd.to_datetime(row['rebalance_date']).strftime('%m/%d/%Y'),
                        'Symbol': str(row['stock_code']),  # Ensure string type to preserve leading zeros
                        'Position Wt.': round(individual_weight, 2),  # Store as number with 2 decimal places
                        'Cost Price': '',  # Empty as requested
                        'Margin trading or not': '',  # Empty as requested
                        # Additional metadata for reference (can be hidden in Excel)
                        'prediction_confidence': row['prediction_confidence'],
                        'class_prob': row.get('class_prob', row['prediction_confidence']),
                        'signal_strength': row.get('signal_strength', row['prediction_confidence']),
                        'rank_in_rebalance': row.get('rank_in_rebalance_class', 1),
                        'original_date': row['date'],
                        'predicted_label': row['predicted_label'],
                        'true_label': row.get('true_label', ''),
                    }
                    trading_positions.append(trading_position)
            
            trading_df = pd.DataFrame(trading_positions)
            
            if len(trading_df) == 0:
                print(f"   ❌ No trading positions generated!")
                return
            
            print(f"   Generated trading positions: {len(trading_df):,}")
            print(f"   Rebalance dates: {trading_df['Adjusment Date'].nunique()}")
            print(f"   Unique symbols: {trading_df['Symbol'].nunique()}")
            print(f"   Date format: mm/dd/yyyy (e.g., {trading_df['Adjusment Date'].iloc[0]})")
            print(f"   Symbol format: String with leading zeros preserved (e.g., {trading_df['Symbol'].iloc[0]})")
            
            # Show weight distribution info
            if auto_weight_distribution and position_weight_pct is None:
                weights_by_date = trading_df.groupby('Adjusment Date')['Position Wt.'].agg(['count', 'sum', 'mean'])
                print(f"   Weight distribution: AUTO (evenly distributed)")
                print(f"   Sample weights by date:")
                for date, stats in weights_by_date.head(3).iterrows():
                    print(f"     {date}: {stats['count']} positions, {stats['sum']:.2f}% total, {stats['mean']:.2f}% each")
            else:
                print(f"   Weight format: {trading_df['Position Wt.'].iloc[0]:.2f}% (fixed per position)")
            
            # Create summary statistics
            summary_stats = []
            for adj_date in trading_df['Adjusment Date'].unique():
                date_positions = trading_df[trading_df['Adjusment Date'] == adj_date]
                stats = {
                    'Adjusment Date': str(adj_date),  # Ensure date is string in mm/dd/yyyy format
                    'Number of Positions': len(date_positions),
                    'Total Weight': f"{date_positions['Position Wt.'].sum():.2f}%",
                    'Avg Weight per Position': f"{date_positions['Position Wt.'].mean():.2f}%",
                    'Avg Confidence': f"{date_positions['prediction_confidence'].mean():.4f}",
                    'Min Confidence': f"{date_positions['prediction_confidence'].min():.4f}",
                    'Max Confidence': f"{date_positions['prediction_confidence'].max():.4f}",
                    'Unique Symbols': date_positions['Symbol'].nunique()
                }
                summary_stats.append(stats)
            
            summary_df = pd.DataFrame(summary_stats)
            
            # Create metadata
            metadata = {
                'creation_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source_portfolio_file': portfolio_excel_path,
                'target_class': target_class,
                'top_n_per_date': top_n_per_date,
                'position_weight_pct': position_weight_pct,
                'auto_weight_distribution': auto_weight_distribution,
                'total_positions': len(trading_df),
                'total_rebalance_dates': trading_df['Adjusment Date'].nunique(),
                'unique_symbols': trading_df['Symbol'].nunique(),
                'date_range_start': trading_df['Adjusment Date'].min(),
                'date_range_end': trading_df['Adjusment Date'].max(),
            }
            
            metadata_df = pd.DataFrame([metadata])
            
            # Save to Excel with multiple sheets
            print(f"   💾 Saving trading format to {output_excel_path}...")
            
            with pd.ExcelWriter(output_excel_path, engine='openpyxl') as writer:
                # Main trading positions sheet (clean format)
                trading_clean_df = trading_df[['Adjusment Date', 'Symbol', 'Position Wt.', 'Cost Price', 'Margin trading or not']].copy()
                
                # Ensure Symbol is explicitly string type to preserve leading zeros
                trading_clean_df['Symbol'] = trading_clean_df['Symbol'].astype(str)
                
                trading_clean_df.to_excel(writer, sheet_name='Trading_Positions', index=False)
                
                # Format columns in Excel
                worksheet = writer.sheets['Trading_Positions']
                
                # Set Symbol column (column B) to text format
                for row in range(2, len(trading_clean_df) + 2):  # Start from row 2 (after header)
                    cell = worksheet[f'B{row}']  # Column B is Symbol
                    cell.number_format = '@'  # Text format
                
                # Set Position Wt. column (column C) to percentage format with 2 decimals
                for row in range(2, len(trading_clean_df) + 2):
                    cell = worksheet[f'C{row}']  # Column C is Position Wt.
                    cell.number_format = '0.00%'  # Percentage format with 2 decimal places
                    cell.value = trading_clean_df.iloc[row-2]['Position Wt.'] / 100  # Convert to decimal for Excel percentage
                
                # Detailed trading positions with metadata
                trading_df_copy = trading_df.copy()
                trading_df_copy['Symbol'] = trading_df_copy['Symbol'].astype(str)
                trading_df_copy.to_excel(writer, sheet_name='Detailed_Positions', index=False)
                
                # Format columns in detailed sheet too
                detailed_worksheet = writer.sheets['Detailed_Positions']
                
                # Format Symbol column (find its position in detailed sheet)
                detailed_cols = list(trading_df_copy.columns)
                if 'Symbol' in detailed_cols:
                    symbol_col_idx = detailed_cols.index('Symbol')
                    symbol_col_letter = chr(65 + symbol_col_idx)
                    for row in range(2, len(trading_df_copy) + 2):
                        cell = detailed_worksheet[f'{symbol_col_letter}{row}']
                        cell.number_format = '@'  # Text format
                
                # Format Position Wt. column in detailed sheet
                if 'Position Wt.' in detailed_cols:
                    weight_col_idx = detailed_cols.index('Position Wt.')
                    weight_col_letter = chr(65 + weight_col_idx)
                    for row in range(2, len(trading_df_copy) + 2):
                        cell = detailed_worksheet[f'{weight_col_letter}{row}']
                        cell.number_format = '0.00%'  # Percentage format
                        cell.value = trading_df_copy.iloc[row-2]['Position Wt.'] / 100
                
                # Summary by date
                summary_df.to_excel(writer, sheet_name='Daily_Summary', index=False)
                
                # Metadata
                metadata_df.to_excel(writer, sheet_name='Metadata', index=False)
            
            print(f"   ✅ Trading format Excel saved successfully!")
            print(f"   📊 Excel contains 4 sheets:")
            print(f"      - Trading_Positions: Clean format for trading (5 columns)")
            print(f"      - Detailed_Positions: Full data with confidence metrics")
            print(f"      - Daily_Summary: Statistics by adjustment date")
            print(f"      - Metadata: File creation information")
            print(f"   📈 Portfolio Summary:")
            print(f"      - Total positions: {len(trading_df):,}")
            print(f"      - Rebalance dates: {trading_df['Adjusment Date'].nunique()}")
            print(f"      - Avg positions per date: {len(trading_df) / trading_df['Adjusment Date'].nunique():.1f}")
            if auto_weight_distribution and position_weight_pct is None:
                # Show auto-distribution info
                sample_weights = trading_df.groupby('Adjusment Date')['Position Wt.'].agg(['count', 'sum']).head(1)
                if not sample_weights.empty:
                    sample_date = sample_weights.index[0]
                    sample_count = sample_weights.iloc[0]['count']
                    sample_total = sample_weights.iloc[0]['sum']
                    sample_avg = sample_weights.iloc[0]['sum'] / sample_weights.iloc[0]['count']
                    print(f"      - Weight distribution: AUTO (evenly distributed)")
                    print(f"      - Sample date {sample_date}: {sample_count} positions, {sample_total:.2f}% total, {sample_avg:.2f}% each")
            else:
                print(f"      - Weight per position: {position_weight_pct}%")
                if top_n_per_date is not None:
                    print(f"      - Total weight per date: {top_n_per_date * position_weight_pct}%")
            print(f"   🔧 Format Specifications:")
            print(f"      - Date format: mm/dd/yyyy (e.g., {trading_df['Adjusment Date'].iloc[0]})")
            print(f"      - Symbol format: String with leading zeros (e.g., {trading_df['Symbol'].iloc[0]})")
            print(f"      - Weight format: {trading_df['Position Wt.'].iloc[0]:.2f}% (Excel percentage with 2 decimals)")
            print(f"      - Excel formatting: Symbol as text, Weight as percentage")
            
            return trading_df
            
        except FileNotFoundError:
            print(f"   ❌ Portfolio file not found: {portfolio_excel_path}")
            print(f"   💡 Make sure to run the main prediction script first to generate the portfolio file")
            return None
        except Exception as e:
            print(f"   ❌ Error creating trading format: {e}")
            return None


# =============================================================================
# 🔧 CONFIGURATION - Edit these variables to customize the prediction run
# =============================================================================

# Path to your model configuration file
CONFIG_PATH = "configs/models/transformer/encoder_only_classification.yaml"

# Path to your trained model checkpoint (using custom preset by default)
CHECKPOINT_PATH = "outputs/encoder_only_transformer_classification_vd_20190101_20211231_t_20080101_20181231_factors_19_TorchTransformerClassification_custom_20250825_144231/ckpt/checkpoint_epoch_100.pth"

# Default preset to use (models are typically trained with 'custom' preset)
DEFAULT_PRESET = "custom"

# Output Excel file paths
SAMPLE_EXCEL_PATH = "model_predictions_sample.xlsx"  # 0.1% sample for inspection
PORTFOLIO_EXCEL_PATH = "model_predictions_portfolio.xlsx"  # Processed for portfolio optimization

# Sample fraction (0.001 = 0.1%)
SAMPLE_FRACTION = 0.001

# Portfolio optimization configuration
PORTFOLIO_CONFIG = {
    'top_n_per_class': None,          # Select top B predictions per class per rebalance date (None or -1 = select all)
    'rebalance_frequency_days': 10,  # Rebalance every 10 days
    'min_confidence': 0.02,          # Minimum prediction confidence threshold
    'ignore_min_confidence': True,  # If True, bypass confidence filtering to select all predictions
    'splits_to_include': ['train', 'valid', 'test'],   # Which data splits to use ('train', 'valid', 'test')
    'sort_by_confidence': True,      # Sort by confidence (True) or accuracy (False)
    'target_classes': 11           # Classes to include: None=all, [0,1,49]=specific, (40,49)=range 40-49 inclusive
}

# Trading format configuration
CREATE_TRADING_FORMAT = True        # Whether to create trading format Excel
TRADING_EXCEL_PATH = "trading_positions.xlsx"  # Output path for trading Excel
TRADING_CONFIG = {
    'target_class': 11,              # Class to focus on for trading (49 = highest return)
    'top_n_per_date': 50,           # Number of positions per rebalance date (None = all available)
    'position_weight_pct': None,     # Weight percentage for each position (None = auto-distribute evenly)
    'auto_weight_distribution': True # Automatically distribute weights evenly across positions
}

# =============================================================================


def _set_config_preset(config_path: str, preset: str):
    """Temporarily set the preset in the config file for prediction."""
    try:
        import yaml
        # Read current config
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # Update preset
        original_preset = config.get('preset', 'medium')
        if original_preset != preset:
            config['preset'] = preset
            
            # Save updated config
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False)
            
            print(f"🔧 Updated config preset from '{original_preset}' to '{preset}'")
        else:
            print(f"✅ Config already using preset '{preset}'")
            
    except Exception as e:
        print(f"⚠️  Could not update config preset: {e}")
        print(f"💡 Manually set preset: '{preset}' in {config_path}")


def main():
    multiprocessing.set_start_method('spawn', force=True)
    """Main function to run comprehensive predictions."""
    
    # Check if files exist
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ Configuration file not found: {CONFIG_PATH}")
        print("💡 Please update CONFIG_PATH in this script to point to your config file")
        return
    
    if not os.path.exists(CHECKPOINT_PATH):
        print(f"❌ Checkpoint file not found: {CHECKPOINT_PATH}")
        print("💡 Please update CHECKPOINT_PATH in this script to point to your checkpoint")
        return
    
    print(f"🚀 Starting comprehensive model prediction run...")
    print(f"   Config: {CONFIG_PATH}")
    print(f"   Checkpoint: {CHECKPOINT_PATH}")
    print(f"   Sample Excel: {SAMPLE_EXCEL_PATH} ({SAMPLE_FRACTION:.1%} of data)")
    print(f"   Portfolio Excel: {PORTFOLIO_EXCEL_PATH}")
    print(f"   Using preset: {DEFAULT_PRESET}")
    print(f"\n📊 Portfolio Configuration:")
    top_n = PORTFOLIO_CONFIG['top_n_per_class']
    print(f"   - top_n_per_class (B): {top_n if top_n not in [None, -1] else 'ALL (no limit)'}")
    print(f"   - rebalance_frequency_days: {PORTFOLIO_CONFIG['rebalance_frequency_days']}")
    print(f"   - min_confidence: {PORTFOLIO_CONFIG['min_confidence']}")
    print(f"   - ignore_min_confidence: {PORTFOLIO_CONFIG['ignore_min_confidence']}")
    print(f"   - splits_to_include: {PORTFOLIO_CONFIG['splits_to_include']}")
    print(f"   - sort_by_confidence: {PORTFOLIO_CONFIG['sort_by_confidence']}")
    print(f"   - target_classes: {PORTFOLIO_CONFIG['target_classes']}")
    
    if PORTFOLIO_CONFIG['ignore_min_confidence']:
        print(f"   Selection mode: ALL predictions (confidence ignored)")
    elif top_n in [None, -1]:
        print(f"   Selection mode: ALL predictions per class (confidence filtered)")
    else:
        print(f"   Expected entries: A target classes × B predictions × rebalance dates")
    
    if CREATE_TRADING_FORMAT:
        print(f"\n📈 Trading Format Configuration:")
        print(f"   - create_trading_format: {CREATE_TRADING_FORMAT}")
        print(f"   - trading_excel_path: {TRADING_EXCEL_PATH}")
        print(f"   - target_class: {TRADING_CONFIG['target_class']}")
        top_n_trading = TRADING_CONFIG['top_n_per_date']
        print(f"   - top_n_per_date: {top_n_trading if top_n_trading not in [None, -1] else 'ALL available'}")
        print(f"   - position_weight_pct: {TRADING_CONFIG['position_weight_pct'] if TRADING_CONFIG['position_weight_pct'] is not None else 'AUTO-distribute'}")
        print(f"   - auto_weight_distribution: {TRADING_CONFIG['auto_weight_distribution']}")
        
        if TRADING_CONFIG['auto_weight_distribution'] and TRADING_CONFIG['position_weight_pct'] is None:
            print(f"   Expected trading positions: {top_n_trading if top_n_trading not in [None, -1] else 'ALL'} positions × rebalance dates (weights auto-distributed)")
        else:
            print(f"   Expected trading positions: {top_n_trading if top_n_trading not in [None, -1] else 'ALL'} positions × rebalance dates (fixed {TRADING_CONFIG['position_weight_pct']}% each)")
        print(f"   Date format: mm/dd/yyyy, Weight format: percentage with 2 decimals")
    
    # Set the preset in config file temporarily
    _set_config_preset(CONFIG_PATH, DEFAULT_PRESET)
    
    # Create prediction runner
    runner = ModelPredictionRunner(CONFIG_PATH, CHECKPOINT_PATH)
    
    # Generate all predictions
    all_predictions, summary_stats, class_dist, metadata, portfolio_predictions = runner.generate_all_predictions(
        sample_output_path=SAMPLE_EXCEL_PATH,
        portfolio_output_path=PORTFOLIO_EXCEL_PATH,
        sample_fraction=SAMPLE_FRACTION,
        portfolio_config=PORTFOLIO_CONFIG
    )
    
    print(f"\n🎉 Prediction run completed successfully!")
    print(f"📁 Sample file: {SAMPLE_EXCEL_PATH} ({len(all_predictions) * SAMPLE_FRACTION:.0f} samples)")
    print(f"📁 Portfolio file: {PORTFOLIO_EXCEL_PATH} ({len(portfolio_predictions):,} optimized predictions)")
    print(f"📊 Total predictions generated: {len(all_predictions):,}")
    
    # Create trading format Excel if requested
    if CREATE_TRADING_FORMAT and len(portfolio_predictions) > 0:
        print(f"\n📈 Creating trading format Excel...")
        print(f"   Target class: {TRADING_CONFIG['target_class']}")
        top_n_trading = TRADING_CONFIG['top_n_per_date']
        print(f"   Positions per date: {top_n_trading if top_n_trading not in [None, -1] else 'ALL available'}")
        weight_pct = TRADING_CONFIG['position_weight_pct']
        if weight_pct is None and TRADING_CONFIG['auto_weight_distribution']:
            print(f"   Weight per position: AUTO-distribute (evenly across positions)")
        else:
            print(f"   Weight per position: {weight_pct}%")
        
        trading_df = runner.create_trading_format_excel(
            portfolio_excel_path=PORTFOLIO_EXCEL_PATH,
            output_excel_path=TRADING_EXCEL_PATH,
            target_class=TRADING_CONFIG['target_class'],
            top_n_per_date=TRADING_CONFIG['top_n_per_date'],
            position_weight_pct=TRADING_CONFIG['position_weight_pct'],
            auto_weight_distribution=TRADING_CONFIG['auto_weight_distribution']
        )
        
        if trading_df is not None:
            print(f"📁 Trading file: {TRADING_EXCEL_PATH} ({len(trading_df):,} positions)")
    elif CREATE_TRADING_FORMAT and len(portfolio_predictions) == 0:
        print(f"\n⚠️  Skipping trading format creation - no portfolio predictions available")


def create_trading_format_standalone(portfolio_excel_path: str = PORTFOLIO_EXCEL_PATH, 
                                    output_excel_path: str = TRADING_EXCEL_PATH,
                                    config_path: str = CONFIG_PATH):
    """
    Standalone function to create trading format Excel from existing portfolio file.
    
    Args:
        portfolio_excel_path: Path to existing portfolio Excel file
        output_excel_path: Path for trading format Excel output  
        config_path: Path to model config (needed for ModelPredictionRunner initialization)
    """
    print(f"📈 Creating trading format Excel from existing portfolio...")
    print(f"   Portfolio file: {portfolio_excel_path}")
    print(f"   Trading file: {output_excel_path}")
    
    # Create a minimal runner instance (we only need the trading format method)
    try:
        # We need to create a runner instance, but we don't need to load the model
        # Just use dummy checkpoint path since we're not running predictions
        dummy_runner = ModelPredictionRunner.__new__(ModelPredictionRunner)
        dummy_runner.config_path = config_path
        
        # Call the trading format method
        trading_df = dummy_runner.create_trading_format_excel(
            portfolio_excel_path=portfolio_excel_path,
            output_excel_path=output_excel_path,
            target_class=TRADING_CONFIG['target_class'],
            top_n_per_date=TRADING_CONFIG['top_n_per_date'],
            position_weight_pct=TRADING_CONFIG['position_weight_pct'],
            auto_weight_distribution=TRADING_CONFIG['auto_weight_distribution']
        )
        
        return trading_df
        
    except Exception as e:
        print(f"❌ Error in standalone trading format creation: {e}")
        return None


if __name__ == "__main__":
    import sys
    
    # Check if user wants to create trading format only
    if len(sys.argv) > 1 and sys.argv[1] == "--trading-only":
        print("🎯 Running in trading-format-only mode...")
        create_trading_format_standalone()
    else:
        # Run the full prediction pipeline
        main()
