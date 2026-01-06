#!/usr/bin/env python3
"""
Script to train PyTorch nn.Transformer Based Encoder-Only Model with different presets

This script demonstrates how to train the PyTorch nn.Transformer based encoder-only 
transformer model using different preset configurations (small, medium, large).
"""

import os
import sys
import yaml
import shutil
import multiprocessing
from pathlib import Path

# Add the src directory to the Python path
sys.path.append(str(Path(__file__).parent / "src"))

from train_torch_transformer import TorchTransformerTrainer


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


def train_with_preset(preset: str, config_path: str = "configs/models/transformer/encoder_only.yaml"):
    """Train the PyTorch nn.Transformer model with a specific preset."""
    print(f"\n🚀 Training PyTorch nn.Transformer with {preset} preset...")
    
    # Update the config file to use the specified preset
    update_preset_in_config(config_path, preset)
    
    # Date ranges are already configured in the config file by default
    # No need to modify them - they will be used automatically
    
    # Create trainer and start training
    trainer = TorchTransformerTrainer(config_path)
    trainer.train()
    
    print(f"✅ Training with {preset} preset completed!")


def show_preset_details(config_path: str):
    """Show details of all available presets."""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    presets = config.get('presets', {})
    
    print("\n📋 Available Presets Details:")
    print("=" * 60)
    
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
    
    print(f"\n🏷️  Preset Configurations:")
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
        
        if 'training' in preset_config:
            train = preset_config['training']
            print(f"   Training:")
            print(f"     - batch_size: {train.get('batch_size', 'N/A')}")
            print(f"     - epochs: {train.get('epochs', 'N/A')}")
            print(f"     - learning_rate: {train.get('optimizer', {}).get('learning_rate', 'N/A')}")
            print(f"     - optimizer: {train.get('optimizer', {}).get('type', 'N/A')}")
            
            # Display noise configuration
            noise_config = train.get('noise', {})
            noise_enabled = noise_config.get('enabled', False)
            if noise_enabled:
                print(f"     - noise: Enabled (std={noise_config.get('gaussian_std', 'N/A')})")
            else:
                print(f"     - noise: Disabled")


def main():
    """Main function to demonstrate preset training."""
    config_path = "configs/models/transformer/encoder_only.yaml"
    
    if not os.path.exists(config_path):
        print(f"❌ Configuration file not found: {config_path}")
        return
    
    # Available presets
    presets = ["custom", "small", "medium", "large"]
    
    print("🎯 PyTorch nn.Transformer Encoder-Only Preset Training")
    print("=" * 60)
    print("Available options:")
    for i, preset in enumerate(presets, 1):
        print(f"  {i}. Train with {preset} preset")
    print("  5. Train all presets sequentially")
    print("  6. Show preset details")
    print("  7. Custom configuration")
    
    try:
        choice = input("\nSelect option (1-7): ").strip()
        
        if choice == "1":
            train_with_preset("custom", config_path)
        elif choice == "2":
            train_with_preset("small", config_path)
        elif choice == "3":
            train_with_preset("medium", config_path)
        elif choice == "4":
            train_with_preset("large", config_path)
        elif choice == "5":
            print("\n🔄 Training all presets sequentially...")
            for preset in presets:
                print(f"\n{'='*20} Training {preset.upper()} preset {'='*20}")
                train_with_preset(preset, config_path)
                if preset != presets[-1]:  # Not the last preset
                    print(f"\n⏸️  Completed {preset} preset. Press Enter to continue to next preset...")
                    input()
        elif choice == "6":
            show_preset_details(config_path)
        elif choice == "7":
            print("\n🔧 Using custom configuration...")
            update_preset_in_config(config_path, "custom")
            trainer = TorchTransformerTrainer(config_path)
            trainer.train()
        else:
            print("❌ Invalid choice. Please select 1-7.")
            
    except KeyboardInterrupt:
        print("\n⏹️  Training interrupted by user.")
    except Exception as e:
        print(f"❌ Error during training: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # 🚀 Fix CUDA multiprocessing: use 'spawn' instead of 'fork' for DataLoader workers
    try:
        multiprocessing.set_start_method('spawn', force=True)
        print("✅ Set multiprocessing start method to 'spawn' for CUDA compatibility")
    except RuntimeError:
        # If start method is already set, just continue
        print("⚠️  Multiprocessing start method already set")
    
    main() 