"""
Main data pipeline for the quant framework.
"""
from typing import Dict, Any, Optional
import pandas as pd
from src.utils.logger import setup_logger

from .data_loading.market_data import load_market_data
from .data_loading.financial_data import load_fundamental_data
from .data_engineering.features_engineering import create_features
from .data_engineering.labels_engineering import create_labels
from .preprocessing import DataPipeline
from .data_saving.data_to_testdb import save_to_db

class DataServicePipeline:
    """Main data pipeline for the quant framework."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the data service pipeline.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        self.logger = setup_logger("data_pipeline")
        self.preprocessing_pipeline = DataPipeline.create_default_pipeline()
    
    def load_data(self) -> Dict[str, pd.DataFrame]:
        """
        Load all required data.
        
        Returns:
            Dictionary of loaded dataframes
        """
        self.logger.info("Loading market data...")
        market_data = load_market_data()
        
        self.logger.info("Loading fundamental data...")
        fundamental_data = load_fundamental_data()
        
        return {
            'market_data': market_data,
            'fundamental_data': fundamental_data
        }
    
    def preprocess_data(self, data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        Preprocess all data.
        
        Args:
            data: Dictionary of dataframes to preprocess
            
        Returns:
            Dictionary of preprocessed dataframes
        """
        self.logger.info("Preprocessing data...")
        processed_data = {}
        
        for name, df in data.items():
            self.logger.info(f"Preprocessing {name}...")
            processed_data[name] = self.preprocessing_pipeline.fit_transform(df)
            
        return processed_data
    
    def create_features_and_labels(
        self,
        data: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        Create features and labels from preprocessed data.
        
        Args:
            data: Dictionary of preprocessed dataframes
            
        Returns:
            Dictionary containing features and labels
        """
        self.logger.info("Creating features...")
        features = create_features(data)
        
        self.logger.info("Creating labels...")
        labels = create_labels(data)
        
        return {
            'features': features,
            'labels': labels
        }
    
    def save_data(self, data: Dict[str, pd.DataFrame]) -> None:
        """
        Save processed data to database.
        
        Args:
            data: Dictionary of dataframes to save
        """
        self.logger.info("Saving data to database...")
        save_to_db(data)
    
    def run(self) -> None:
        """Run the complete data pipeline."""
        try:
            # Load data
            raw_data = self.load_data()
            
            # Preprocess data
            processed_data = self.preprocess_data(raw_data)
            
            # Create features and labels
            features_and_labels = self.create_features_and_labels(processed_data)
            
            # Save data
            self.save_data(features_and_labels)
            
            self.logger.info("Data pipeline completed successfully")
            
        except Exception as e:
            self.logger.error(f"Data pipeline failed: {str(e)}")
            raise 