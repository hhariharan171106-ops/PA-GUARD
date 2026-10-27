from datasets import load_dataset

dataset_name = "ekacare/medical_records_parsing_validation_set"

print("Loading Eka medical records dataset...")

ds = load_dataset(dataset_name)

print("\nDataset loaded successfully!")
print(ds)

for split_name, split_data in ds.items():
    print(f"\nSplit: {split_name}")
    print(f"Number of examples: {len(split_data)}")
    print("Columns:", split_data.column_names)

    if len(split_data) > 0:
        print("\nFirst example:")
        example = split_data[0]

        for key, value in example.items():
            if key == "image":
                print(f"{key}: {value}")
            else:
                print(f"{key}: {str(value)[:500]}")