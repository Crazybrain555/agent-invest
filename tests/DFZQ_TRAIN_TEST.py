# DFZQ_TRAIN_TEST.py (Root directory test script)

import logging
from pathlib import Path

# Important: Ensure src is in PYTHONPATH or adjust imports accordingly
# For example, if DFZQ_TRAIN_TEST.py is in the root and src is a subdir:
# import sys
# sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from src.train.Neural_networks.RNN.DFZQ_GRU.train_dfzq_gru import run_training
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig

if __name__ == "__main__":
    print("Starting test run of train_dfzq_gru.py...")

    # 1. Create a specific TrainingConfig for this test run
    test_cfg = TrainingConfig()

    # 2. Override parameters for a quick test
    test_cfg.output_root = "outputs/DFZQ_GRU_MODEL_QUICK_TEST"
    test_cfg.max_epochs = 3  # Run for only a few epochs
    test_cfg.patience = 2    # Early stop quickly if no improvement
    
    # Limit samples for faster testing (e.g., 2 batches for train, 1 for valid/test)
    # Assuming batch_size is 256 by default in TrainingConfig
    test_cfg.max_samples_train = 512 
    test_cfg.max_samples_valid = 256
    test_cfg.max_samples_test = 256
    
    test_cfg.num_workers = 0  # Use 0 workers for easier debugging in tests
    test_cfg.batch_size = 64 # Smaller batch size for faster iteration in this test
    
    # You can also force CPU for testing if GPU is problematic or for consistency
    # test_cfg.force_cpu = True 

    # Ensure the dataset path is correct relative to your workspace root
    # test_cfg.dataset_path = "data/Dataset/pv_v1" # Default is usually fine

    print(f"Using temporary output root: {test_cfg.output_root}")
    print(f"Max epochs: {test_cfg.max_epochs}")
    print(f"Max samples (train/valid/test): {test_cfg.max_samples_train}/{test_cfg.max_samples_valid}/{test_cfg.max_samples_test}")
    print(f"Batch size for test: {test_cfg.batch_size}")

    # 3. Run the training logic with the test configuration
    try:
        run_training(test_cfg)
        print("Test run of train_dfzq_gru.py completed successfully.")
        print(f"Check outputs in: {Path(test_cfg.output_root).resolve()}")
    except Exception as e:
        logging.exception("Error during test run of train_dfzq_gru.py")
        print(f"Test run failed. Error: {e}")

    # --- Original Dataloader tests (can be kept or removed) ---
    # print("\n--- Original Dataloader Sanity Checks ---")
    # from src.dataset.DFZQ_GRU_PV_dataset.parquet_pv_dataset import ParquetPVDataset
    # from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_dataloader, get_train_valid_test_loaders
    
    # dataset_path_original = Path("data/Dataset/pv_v1")
    # print("exists:", dataset_path_original.exists())

    # # 2. Directly iterate Dataset (single sample)
    # if dataset_path_original.exists():
    #     try:
    #         ds = ParquetPVDataset(root=dataset_path_original, split="train", shuffle=True, seed=42, chunk_size=64, max_samples=10)
    #         it = iter(ds)
    #         x, y, date, code = next(it)
    #         print("sample:", x.shape, y, date, code)
    #     except Exception as e:
    #         print(f"Error in single sample test: {e}")

    #     # 3. DataLoader batch mode test: no meta
    #     try:
    #         loader_cfg = {
    #             "dataset_path": str(dataset_path_original),
    #             "batch_size": 16,
    #             "num_workers": 0,
    #             "shuffle": True,
    #             "seed": 42,
    #             "max_samples": 64 # Limit samples for this loader test
    #         }
    #         loader = get_dataloader("train", loader_cfg, keep_meta=False)
    #         feats, labels = next(iter(loader))
    #         print("batch without meta:", feats.shape, labels.shape)
    #     except Exception as e:
    #         print(f"Error in dataloader (no meta) test: {e}")
