#!/usr/bin/env python3
"""
Script to train PyTorch nn.Transformer Based Encoder-Only Classification Model with different presets

This script demonstrates how to train the PyTorch nn.Transformer based encoder-only 
transformer classification model using different preset configurations (binary, small, medium, large, custom).
"""

import os
import sys
import yaml
import shutil
import multiprocessing
from pathlib import Path

# Add the src directory to the Python path
sys.path.append(str(Path(__file__).parent / "src"))

from train_torch_transformer_classification import TorchTransformerClassificationTrainer


def update_preset_in_config(config_path: str, preset: str):
    """Update the preset in the configuration file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    config['preset'] = preset
    
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    
    print(f"✅ Updated config to use preset: {preset}")


def update_date_ranges_in_config(config_path: str, date_ranges: dict, use_custom_splits: bool = True):
    """Update the date ranges in the configuration file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # Ensure data section exists
    if 'data' not in config:
        config['data'] = {}
    
    config['data']['date_ranges'] = date_ranges
    config['data']['use_custom_splits'] = use_custom_splits
    
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    
    print(f"✅ Updated config with date ranges: {date_ranges}")


def update_factors_in_config(config_path: str, selected_factors: list):
    """Update the selected factors in the configuration file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # Ensure data section exists
    if 'data' not in config:
        config['data'] = {}
    
    config['data']['selected_factors'] = selected_factors
    
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    
    print(f"✅ Updated config with {len(selected_factors)} selected factors")


def train_with_preset(preset: str, config_path: str = "configs/models/transformer/encoder_only_classification.yaml"):
    """Train the PyTorch nn.Transformer classification model with a specific preset."""
    print(f"\n🚀 Training PyTorch nn.Transformer Classification with {preset} preset...")
    
    # Update the config file to use the specified preset
    update_preset_in_config(config_path, preset)
    
    # Date ranges are already configured in the config file by default
    # No need to modify them - they will be used automatically
    
    # Create trainer and start training
    trainer = TorchTransformerClassificationTrainer(config_path)
    trainer.train()
    
    print(f"✅ Training with {preset} preset completed!")


def show_preset_details(config_path: str):
    """Show details of all available presets."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    presets = config.get('presets', {})
    
    print("\n📋 Available Classification Presets Details:")
    print("=" * 70)
    
    # Show current data configuration
    data_config = config.get('data', {})
    print(f"\n🗓️  Current Data Configuration:")
    print(f"   Dataset: {data_config.get('dataset_path', 'N/A')}")
    print(f"   Use Custom Splits: {data_config.get('use_custom_splits', True)} (default: enabled)")
    if data_config.get('date_ranges'):
        date_ranges = data_config['date_ranges']
        print(f"   Date Ranges:")
        for split, dates in date_ranges.items():
            print(f"     - {split}: {dates[0]} to {dates[1]}")
    else:
        print(f"   Date Ranges: Using dataset default splits")
    
    # Show selected factors
    selected_factors = data_config.get('selected_factors', [])
    if selected_factors:
        print(f"   Selected Factors: {len(selected_factors)} factors")
        print(f"     - {', '.join(selected_factors[:5])}{'...' if len(selected_factors) > 5 else ''}")
    else:
        print(f"   Selected Factors: Using all available factors")
    
    print(f"\n🏷️  Classification Preset Configurations:")
    for preset_name, preset_config in presets.items():
        print(f"\n🏷️  {preset_name.upper()} Preset:")
        print(f"   Description: {preset_config.get('description', 'No description')}")
        
        if 'architecture' in preset_config:
            arch = preset_config['architecture']
            print(f"   Architecture:")
            print(f"     - d_model: {arch.get('d_model', 'N/A')}")
            print(f"     - num_encoder_layers: {arch.get('num_encoder_layers', 'N/A')}")
            print(f"     - nhead: {arch.get('nhead', 'N/A')}")
            print(f"     - dim_feedforward: {arch.get('dim_feedforward', 'N/A')}")
            print(f"     - dropout: {arch.get('dropout', 'N/A')}")
            print(f"     - num_classes: {arch.get('num_classes', 'N/A')}")
            print(f"     - feature_dim: {arch.get('feature_dim', 'N/A')}")
            print(f"     - pooling: {arch.get('pooling', 'N/A')}")
        
        if 'training' in preset_config:
            train = preset_config['training']
            print(f"   Training:")
            print(f"     - batch_size: {train.get('batch_size', 'N/A')}")
            print(f"     - epochs: {train.get('epochs', 'N/A')}")
            print(f"     - learning_rate: {train.get('optimizer', {}).get('learning_rate', 'N/A')}")
            print(f"     - optimizer: {train.get('optimizer', {}).get('type', 'N/A')}")
            print(f"     - loss_function: {train.get('loss_function', 'N/A')}")
            print(f"     - label_smoothing: {train.get('label_smoothing', 'N/A')}")
            print(f"     - use_focal_loss: {train.get('use_focal_loss', 'N/A')}")
            print(f"     - early_stopping patience: {train.get('early_stopping', {}).get('patience', 'N/A')}")
            
            # Display class weights
            class_weights = train.get('class_weights', None)
            if class_weights:
                print(f"     - class_weights: {class_weights}")
            else:
                print(f"     - class_weights: None (balanced)")
            
            # Display noise configuration
            noise_config = train.get('noise', {})
            noise_enabled = noise_config.get('enabled', False)
            if noise_enabled:
                print(f"     - noise: Enabled (std={noise_config.get('gaussian_std', 'N/A')})")
            else:
                print(f"     - noise: Disabled")


def show_factor_selection_options():
    """Show factor selection options and get user input."""
    print("\n🎯 Factor Selection Options:")
    print("1. Use all available factors (default)")
    print("2. Use preset factors from config")
    print("3. Select basic price factors")
    print("4. Select volume-based factors")
    print("5. Select technical indicators")
    print("6. Custom factor selection")
    
    try:
        choice = input("Select factor option (1-6): ").strip()
        
        if choice == "1":
            return None  # Use all factors
        elif choice == "2":
            return "preset"  # Use factors from config
        elif choice == "3":
            # Basic price factors
            return [
                "adj_close_mar_w1", "adj_open_mar_w1", "adj_high_mar_w1", "adj_low_mar_w1",
                "vwap_mar_w1", "vwap_mar_w30"
            ]
        elif choice == "4":
            # Volume-based factors
            return [
                "amount_mar_w30", "MinuVol_call_w0", "high_vol_close_w0", "high_vol_open_w0",
                "price2vol_w0", "residpos_amount_pm_w0"
            ]
        elif choice == "5":
            # Technical indicators
            return [
                "swing_w0", "High_PVcor_w0", "apm_w0", "high_pvi_w0", 
                "high_vr_w0", "up_down_limit_status_w0"
            ]
        elif choice == "6":
            # Custom selection
            print("\nEnter factor names separated by spaces:")
            factors_input = input("Factors: ").strip()
            if factors_input:
                return factors_input.split()
            else:
                return None
        else:
            print("Invalid choice, using all factors")
            return None
    except:
        print("Invalid input, using all factors")
        return None


def train_with_custom_options(config_path: str):
    """Train with custom options including factor selection."""
    print("\n🔧 Custom Configuration Training")
    
    # Select preset
    print("\nAvailable presets: binary, small, medium, large, custom")
    preset = input("Enter preset name (default: medium): ").strip() or "medium"
    
    # Factor selection
    selected_factors = show_factor_selection_options()
    
    # Update config
    update_preset_in_config(config_path, preset)
    
    if selected_factors == "preset":
        print("✅ Using factors from preset configuration")
    elif selected_factors:
        update_factors_in_config(config_path, selected_factors)
    else:
        print("✅ Using all available factors")
    
    # Start training
    trainer = TorchTransformerClassificationTrainer(config_path)
    trainer.train()


def main():
    """Main function to demonstrate preset training."""
    config_path = "configs/models/transformer/encoder_only_classification.yaml"
    
    if not os.path.exists(config_path):
        print(f"❌ Configuration file not found: {config_path}")
        return
    
    # Available presets
    presets = ["binary", "small", "medium", "large", "custom"]
    
    print("🎯 PyTorch nn.Transformer Encoder-Only Classification Preset Training")
    print("=" * 70)
    print("Available options:")
    for i, preset in enumerate(presets, 1):
        print(f"  {i}. Train with {preset} preset")
    print("  6. Train all presets sequentially")
    print("  7. Show preset details")
    print("  8. Custom configuration with factor selection")
    print("  9. Quick test with binary classification")
    
    try:
        choice = input("\nSelect option (1-9): ").strip()
        
        if choice == "1":
            train_with_preset("binary", config_path)
        elif choice == "2":
            train_with_preset("small", config_path)
        elif choice == "3":
            train_with_preset("medium", config_path)
        elif choice == "4":
            train_with_preset("large", config_path)
        elif choice == "5":
            train_with_preset("custom", config_path)
        elif choice == "6":
            print("\n🔄 Training all presets sequentially...")
            for preset in presets:
                print(f"\n{'='*20} Training {preset.upper()} preset {'='*20}")
                train_with_preset(preset, config_path)
                if preset != presets[-1]:  # Not the last preset
                    print(f"\n⏸️  Completed {preset} preset. Press Enter to continue to next preset...")
                    input()
        elif choice == "7":
            show_preset_details(config_path)
        elif choice == "8":
            train_with_custom_options(config_path)
        elif choice == "9":
            print("\n🚀 Quick test with binary classification...")
            # Set up for quick binary test
            update_preset_in_config(config_path, "binary")
            # Use basic price factors for quick testing
            basic_factors = ["adj_close_mar_w1", "adj_open_mar_w1", "adj_high_mar_w1", "adj_low_mar_w1"]
            update_factors_in_config(config_path, basic_factors)
            
            trainer = TorchTransformerClassificationTrainer(config_path)
            trainer.train()
        else:
            print("❌ Invalid choice. Please select 1-9.")
            
    except KeyboardInterrupt:
        print("\n⏹️  Training interrupted by user.")
    except Exception as e:
        print(f"❌ Error during training: {str(e)}")
        import traceback
        traceback.print_exc()


def show_classification_use_cases():
    """Show different classification use cases and their recommended configurations."""
    print("\n💡 Classification Use Cases & Recommended Presets:")
    print("=" * 60)
    
    use_cases = [
        {
            "name": "Trading Signals (Buy/Sell)",
            "preset": "binary",
            "description": "Simple buy/sell decisions",
            "classes": "2 classes: [0=Sell, 1=Buy]",
            "factors": "Price-based factors",
            "target": "Quick decisions, high frequency"
        },
        {
            "name": "Trading Actions (Buy/Hold/Sell)",
            "preset": "small",
            "description": "Three-way trading decisions",
            "classes": "3 classes: [0=Sell, 1=Hold, 2=Buy]",
            "factors": "All factors",
            "target": "Balanced trading strategy"
        },
        {
            "name": "Return Quintiles",
            "preset": "medium",
            "description": "Rank stocks into 5 performance buckets",
            "classes": "5 classes: [0=Bottom, 1=Low, 2=Mid, 3=High, 4=Top]",
            "factors": "Selected important factors",
            "target": "Portfolio construction"
        },
        {
            "name": "Fine-grained Ranking",
            "preset": "large",
            "description": "Detailed performance classification",
            "classes": "10 classes: [0-9 performance deciles]",
            "factors": "All factors",
            "target": "Precise ranking, research"
        },
        {
            "name": "Custom Research",
            "preset": "custom",
            "description": "Flexible configuration for experiments",
            "classes": "Configurable",
            "factors": "Configurable",
            "target": "Research and experimentation"
        }
    ]
    
    for i, use_case in enumerate(use_cases, 1):
        print(f"\n{i}. {use_case['name']} ({use_case['preset']} preset)")
        print(f"   Description: {use_case['description']}")
        print(f"   Classes: {use_case['classes']}")
        print(f"   Factors: {use_case['factors']}")
        print(f"   Target: {use_case['target']}")
    
    print(f"\n💡 Quick Start Commands:")
    print(f"   python train_torch_transformer_classification_with_preset.py  # Interactive mode")
    print(f"   python train_torch_transformer_classification.py --preset binary")
    print(f"   python train_torch_transformer_classification.py --preset medium")


if __name__ == "__main__":
    # 🚀 Fix CUDA multiprocessing: use 'spawn' instead of 'fork' for DataLoader workers
    try:
        # multiprocessing.set_start_method('spawn', force=True)   #linux 要加
        print("✅ Set multiprocessing start method to 'spawn' for CUDA compatibility")
    except RuntimeError:
        # If start method is already set, just continue
        print("⚠️  Multiprocessing start method already set")
    
    # Show use cases first
    if len(sys.argv) > 1 and sys.argv[1] == "--help-use-cases":
        show_classification_use_cases()
    else:
        main()
