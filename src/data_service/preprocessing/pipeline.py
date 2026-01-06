"""
Data preprocessing pipeline for combining multiple preprocessing steps.
"""
from typing import List, Dict, Any, Optional, Union
import pandas as pd
from .methods.normalizer import DataNormalizer
from .methods.missing_value import MissingValueHandler
from .methods.encoder import DataEncoder
from .methods.outlier import OutlierHandler

class DataPipeline:
    """Pipeline for combining multiple data preprocessing steps."""
    
    def __init__(self):
        """Initialize the data pipeline."""
        self.steps = []
        self.normalizer = DataNormalizer()
        self.missing_handler = MissingValueHandler()
        self.encoder = DataEncoder()
        self.outlier_handler = OutlierHandler()
    
    def add_step(self, step: Dict[str, Any]) -> 'DataPipeline':
        """
        Add a preprocessing step to the pipeline.
        
        Args:
            step: Dictionary containing step configuration
                {
                    'name': str,
                    'method': str,
                    'params': dict
                }
                
        Returns:
            Self for method chaining
        """
        self.steps.append(step)
        return self
    
    def _execute_step(self, data: pd.DataFrame, step: Dict[str, Any]) -> pd.DataFrame:
        """
        Execute a single preprocessing step.
        
        Args:
            data: Input DataFrame
            step: Step configuration
            
        Returns:
            Processed DataFrame
        """
        method = step['method']
        params = step.get('params', {})
        
        if method.startswith('normalize'):
            if method == 'normalize_standard':
                return self.normalizer.standardize(data, **params)
            elif method == 'normalize_minmax':
                return self.normalizer.normalize(data, **params)
            elif method == 'normalize_robust':
                return self.normalizer.robust_scale(data, **params)
                
        elif method.startswith('missing'):
            if method == 'missing_fill':
                return self.missing_handler.fill_missing(data, **params)
            elif method == 'missing_impute':
                return self.missing_handler.impute_missing(data, **params)
            elif method == 'missing_knn':
                return self.missing_handler.knn_impute(data, **params)
                
        elif method.startswith('encode'):
            if method == 'encode_label':
                return self.encoder.label_encode(data, **params)
            elif method == 'encode_onehot':
                return self.encoder.one_hot_encode(data, **params)
            elif method == 'encode_ordinal':
                return self.encoder.ordinal_encode(data, **params)
                
        elif method.startswith('outlier'):
            if method == 'outlier_detect':
                return self.outlier_handler.detect_outliers(data, **params)
            elif method == 'outlier_handle':
                return self.outlier_handler.handle_outliers(data, **params)
            elif method == 'outlier_winsorize':
                return self.outlier_handler.winsorize(data, **params)
                
        else:
            raise ValueError(f"Unknown method: {method}")
    
    def fit_transform(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Execute all preprocessing steps in sequence.
        
        Args:
            data: Input DataFrame
            
        Returns:
            Processed DataFrame
        """
        result = data.copy()
        
        for step in self.steps:
            result = self._execute_step(result, step)
            
        return result
    
    @staticmethod
    def create_default_pipeline() -> 'DataPipeline':
        """
        Create a default preprocessing pipeline.
        
        Returns:
            Configured DataPipeline instance
        """
        pipeline = DataPipeline()
        
        # Add default steps
        pipeline.add_step({
            'name': 'handle_missing',
            'method': 'missing_fill',
            'params': {'method': 'ffill'}
        })
        
        pipeline.add_step({
            'name': 'handle_outliers',
            'method': 'outlier_handle',
            'params': {'method': 'clip', 'threshold': 3.0}
        })
        
        pipeline.add_step({
            'name': 'encode_categorical',
            'method': 'encode_label',
            'params': {}
        })
        
        pipeline.add_step({
            'name': 'normalize_numerical',
            'method': 'normalize_standard',
            'params': {}
        })
        
        return pipeline 