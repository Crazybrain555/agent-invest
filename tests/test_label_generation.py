import pandas as pd
import numpy as np
import logging
import sys
from datetime import datetime
import matplotlib.pyplot as plt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Import necessary components
from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.data_engineering.labels_engineering import LabelGenerator
from src.data_service.database.database_manager import DatabaseManager
from src.tasks.label_generation_task import LabelGenerationTask

def test_top_correlation_label_strategy():
    """
    Test the TopCorrelationLabelStrategy implementation.
    """
    print("\n=== Testing TopCorrelationLabelStrategy ===")
    
    try:
        # Initialize components
        market_data_provider = MarketDataProvider()
        label_generator = LabelGenerator(market_data_provider=market_data_provider)
        
        # Test parameters
        start_date = '2023-01-01'
        end_date = '2023-01-31'
        label_shift = 5
        corr_window = 60
        corr_rank_num = 10
        min_rank_num = 5
        
        # Generate labels using the top correlation strategy
        print(f"\nGenerating labels from {start_date} to {end_date} with label_shift={label_shift}")
        labels_df = label_generator.generate_top_correlation_labels(
            start_date=start_date,
            end_date=end_date,
            label_shift=label_shift,
            corr_window=corr_window,
            corr_rank_num=corr_rank_num,
            min_rank_num=min_rank_num,
            use_db_pct_change=False  # Use calculated pct_change
        )
        
        # Display results
        print("\nGenerated labels shape:", labels_df.shape)
        print("\nSample of generated labels:")
        print(labels_df.head(10))
        
        # Analyze field distribution
        print("\nField distribution:")
        field_counts = labels_df['field_name'].value_counts()
        print(field_counts)
        
        # Basic validation
        if len(labels_df) > 0:
            print("\nLabel generation test PASSED")
        else:
            print("\nLabel generation test FAILED: Empty result")
            
        return labels_df
        
    except Exception as e:
        print(f"\nLabel generation test FAILED: {str(e)}")
        raise

def test_label_generation_task():
    """
    Test the LabelGenerationTask for generating and saving labels.
    """
    print("\n=== Testing LabelGenerationTask ===")
    
    try:
        # Initialize components
        market_data_provider = MarketDataProvider()
        database_manager = DatabaseManager()
        
        # Create the task
        task = LabelGenerationTask(
            market_data_provider=market_data_provider,
            database_manager=database_manager,
            strategy='top_correlation',
            label_shift=10,
            corr_window=60,
            corr_rank_num=10,
            min_rank_num=5
        )
        
        # Test parameters
        start_date = '2023-01-01'
        end_date = '2023-01-15'  # Using a smaller date range for the test
        
        # Execute the task (save_intermediate=False to skip database save during testing)
        print(f"\nExecuting label generation task for {start_date} to {end_date}")
        labels_df = task.execute(
            start_date=start_date,
            end_date=end_date,
            save_intermediate=False  # Don't save to database in this test
        )
        
        # Display results
        print("\nTask execution completed")
        print("\nGenerated labels shape:", labels_df.shape)
        print("\nSample of generated labels:")
        print(labels_df.head(10))
        
        # Calculate statistics
        stats = task.get_label_statistics(labels_df)
        print("\nLabel statistics:")
        for field, field_stats in stats.items():
            print(f"\n{field}:")
            for stat_name, stat_value in field_stats.items():
                print(f"  {stat_name}: {stat_value}")
        
        # Basic validation
        if len(labels_df) > 0:
            print("\nLabel generation task test PASSED")
        else:
            print("\nLabel generation task test FAILED: Empty result")
            
        return labels_df
        
    except Exception as e:
        print(f"\nLabel generation task test FAILED: {str(e)}")
        raise

def visualize_label_distributions(labels_df):
    """
    Create visualizations of label distributions.
    """
    print("\n=== Creating Label Visualizations ===")
    
    try:
        # Create a figure with subplots
        fig, axes = plt.subplots(1, 2, figsize=(16, 6))
        
        # Plot raw labels distribution
        raw_labels = labels_df[labels_df['field_name'] == 'label_raw']['value']
        axes[0].hist(raw_labels, bins=30, alpha=0.7)
        axes[0].set_title('Distribution of Raw Labels')
        axes[0].set_xlabel('Value')
        axes[0].set_ylabel('Frequency')
        
        # Plot adjusted labels distribution
        adj_labels = labels_df[labels_df['field_name'] == 'label_adj']['value']
        axes[1].hist(adj_labels, bins=30, alpha=0.7)
        axes[1].set_title('Distribution of Adjusted Labels')
        axes[1].set_xlabel('Value')
        axes[1].set_ylabel('Frequency')
        
        plt.tight_layout()
        
        # Save the figure
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        plt.savefig(f'label_distributions_{timestamp}.png')
        print(f"\nVisualization saved as label_distributions_{timestamp}.png")
        
        # Optional: display the plot
        plt.show()
        
    except Exception as e:
        print(f"\nVisualization failed: {str(e)}")

if __name__ == "__main__":
    # Run the tests
    print("\n=== Starting Label Generation Tests ===")
    labels_df = test_top_correlation_label_strategy()
    
    if labels_df is not None and len(labels_df) > 0:
        task_labels_df = test_label_generation_task()
        
        # Visualize results if available
        if task_labels_df is not None and len(task_labels_df) > 0:
            visualize_label_distributions(task_labels_df)
    
    print("\n=== Label Generation Tests Completed ===") 