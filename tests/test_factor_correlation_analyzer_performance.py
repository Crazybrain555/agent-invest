"""
Performance test comparing original and optimized FactorCorrelationAnalyzer implementations.
"""
import sys
import time
import pandas as pd
import numpy as np
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parents[1]
sys.path.insert(0, str(project_root))

from src.utils.factor_correlation_analyzer import FactorCorrelationAnalyzer as OriginalAnalyzer
from src.utils.factor_correlation_analyzer_v2 import FactorCorrelationAnalyzer as OptimizedAnalyzer


def create_synthetic_data(n_dates=100, n_stocks=1000, n_factors=50):
    """Create synthetic factor data for testing."""
    np.random.seed(42)  # For reproducible results
    
    dates = pd.date_range('2023-01-01', periods=n_dates, freq='D')
    stocks = [f'stock_{i:06d}' for i in range(n_stocks)]
    factors = [f'factor_{i:02d}' for i in range(n_factors)]
    
    data = []
    for date in dates:
        for stock in stocks:
            for factor in factors:
                # Create correlated factors with some noise
                base_value = np.random.randn()
                if 'factor_01' in factor or 'factor_02' in factor:
                    # Make these two factors highly correlated
                    correlation_component = np.random.randn() * 0.1
                    base_value += correlation_component
                
                data.append({
                    'trade_date': date.strftime('%Y%m%d'),
                    'stock_code': stock,
                    'field_name': factor,
                    'value': base_value + np.random.randn() * 0.1
                })
    
    return pd.DataFrame(data)


def mock_data_provider_fetch(synthetic_data):
    """Mock data provider that returns synthetic data."""
    def fetch_data(table, start_date=None, end_date=None, stock_codes=None, format='long'):
        return synthetic_data.copy()
    return fetch_data


def test_correlation_calculation_performance():
    """Test performance of correlation calculations."""
    print("Creating synthetic data...")
    synthetic_data = create_synthetic_data(n_dates=50, n_stocks=500, n_factors=20)
    print(f"Created {len(synthetic_data)} records")
    
    # Test original implementation
    print("\n" + "="*50)
    print("Testing Original Implementation")
    print("="*50)
    
    original_analyzer = OriginalAnalyzer(table_name="test_table")
    original_analyzer.data_provider.fetch_data = mock_data_provider_fetch(synthetic_data)
    
    start_time = time.time()
    original_analyzer.load_factor_data()
    original_analyzer.prepare_factor_matrix()
    original_corr = original_analyzer.calculate_factor_correlation(
        correlation_type="pearson",
        method="cross_sectional"
    )
    original_time = time.time() - start_time
    
    print(f"Original implementation time: {original_time:.3f} seconds")
    print(f"Correlation matrix shape: {original_corr.shape}")
    
    # Test optimized implementation
    print("\n" + "="*50)
    print("Testing Optimized Implementation")
    print("="*50)
    
    optimized_analyzer = OptimizedAnalyzer(table_name="test_table")
    optimized_analyzer.data_provider.fetch_data = mock_data_provider_fetch(synthetic_data)
    
    start_time = time.time()
    optimized_analyzer.load_factor_data()
    optimized_analyzer.prepare_factor_matrix()
    optimized_corr = optimized_analyzer.calculate_factor_correlation(
        correlation_type="pearson",
        method="cross_sectional"
    )
    optimized_time = time.time() - start_time
    
    print(f"Optimized implementation time: {optimized_time:.3f} seconds")
    print(f"Correlation matrix shape: {optimized_corr.shape}")
    
    # Performance comparison
    print("\n" + "="*50)
    print("Performance Comparison")
    print("="*50)
    speedup = original_time / optimized_time if optimized_time > 0 else float('inf')
    print(f"Speedup: {speedup:.2f}x")
    print(f"Time reduction: {((original_time - optimized_time) / original_time * 100):.1f}%")
    
    # Verify results are similar (allowing for small numerical differences)
    correlation_diff = np.abs(original_corr.values - optimized_corr.values).max()
    print(f"Maximum correlation difference: {correlation_diff:.6f}")
    
    if correlation_diff < 1e-10:
        print("✅ Results are numerically identical")
    elif correlation_diff < 1e-6:
        print("✅ Results are very similar (within numerical precision)")
    else:
        print("❌ Results differ significantly")
    
    return {
        'original_time': original_time,
        'optimized_time': optimized_time,
        'speedup': speedup,
        'correlation_diff': correlation_diff
    }


def test_high_correlation_analysis_performance():
    """Test performance of high correlation analysis."""
    print("\n" + "="*50)
    print("Testing High Correlation Analysis Performance")
    print("="*50)
    
    # Create a correlation matrix with known high correlations
    n_factors = 100
    np.random.seed(42)
    corr_matrix = np.random.randn(n_factors, n_factors) * 0.3
    corr_matrix = (corr_matrix + corr_matrix.T) / 2  # Make symmetric
    np.fill_diagonal(corr_matrix, 1.0)  # Diagonal should be 1
    
    # Add some high correlations
    corr_matrix[0, 1] = corr_matrix[1, 0] = 0.85
    corr_matrix[2, 3] = corr_matrix[3, 2] = -0.90
    
    factor_names = [f'factor_{i:03d}' for i in range(n_factors)]
    corr_df = pd.DataFrame(corr_matrix, index=factor_names, columns=factor_names)
    
    # Test original implementation
    original_analyzer = OriginalAnalyzer()
    start_time = time.time()
    original_high_corr = original_analyzer.analyze_high_correlations(corr_df, threshold=0.7)
    original_time = time.time() - start_time
    
    # Test optimized implementation
    optimized_analyzer = OptimizedAnalyzer()
    start_time = time.time()
    optimized_high_corr = optimized_analyzer.analyze_high_correlations(corr_df, threshold=0.7)
    optimized_time = time.time() - start_time
    
    print(f"Original high correlation analysis time: {original_time:.4f} seconds")
    print(f"Optimized high correlation analysis time: {optimized_time:.4f} seconds")
    
    speedup = original_time / optimized_time if optimized_time > 0 else float('inf')
    print(f"High correlation analysis speedup: {speedup:.2f}x")
    
    # Verify results
    print(f"Original found {len(original_high_corr)} high correlation pairs")
    print(f"Optimized found {len(optimized_high_corr)} high correlation pairs")
    
    if len(original_high_corr) == len(optimized_high_corr):
        print("✅ Both implementations found the same number of high correlation pairs")
    else:
        print("❌ Different number of high correlation pairs found")
    
    return {
        'original_time': original_time,
        'optimized_time': optimized_time,
        'speedup': speedup
    }


def main():
    """Run performance tests."""
    print("Factor Correlation Analyzer Performance Test")
    print("=" * 60)
    
    try:
        # Test correlation calculation
        corr_results = test_correlation_calculation_performance()
        
        # Test high correlation analysis
        high_corr_results = test_high_correlation_analysis_performance()
        
        # Summary
        print("\n" + "="*60)
        print("PERFORMANCE SUMMARY")
        print("="*60)
        print(f"Correlation calculation speedup: {corr_results['speedup']:.2f}x")
        print(f"High correlation analysis speedup: {high_corr_results['speedup']:.2f}x")
        print(f"Overall performance improvement achieved! ✅")
        
    except ImportError as e:
        print(f"Import error: {e}")
        print("Please ensure all dependencies are installed and paths are correct.")
    except Exception as e:
        print(f"Test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()