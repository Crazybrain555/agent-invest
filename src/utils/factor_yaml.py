import pandas as pd
import yaml
from collections import defaultdict

def xlsx_to_yaml(xlsx_file_path, sheet_name, category_column, value_column, output_yaml_path, translation_dict):
    try:

        df = pd.read_excel(xlsx_file_path,sheet_name=sheet_name)

        if category_column not in df.columns:
            raise ValueError(f"category_column={category_column} not in df.columns")
        if value_column not in df.columns:
            raise ValueError(f"value_column={value_column} not in df.columns")

        df_clean = df.dropna(subset=[category_column, value_column])

        yaml_data = defaultdict(list)

        for _, row in df_clean.iterrows():
            category = str(row[category_column]).strip()
            value = str(row[value_column]).strip()


            if category in translation_dict:
                category = translation_dict[category]
            else:
                print(f"category={category} not in translation_dict")

            if value not in yaml_data[category]:
                yaml_data[category].append(value)

        yaml_data = dict(sorted(yaml_data.items()))

        with open(output_yaml_path, "w", encoding='utf-8') as yaml_file:
            class CustomDumper(yaml.SafeDumper):
                def increase_indent(self, flow=False, indentless=False):
                    return super(CustomDumper, self).increase_indent(flow, False)
            yaml.dump(yaml_data, yaml_file, Dumper=CustomDumper, default_flow_style=False,
                      allow_unicode=True, sort_keys=False, indent=2,
                      default_style=None, width=float("inf"))

        print(f"Wrote to {output_yaml_path}")
        print(f"Found {len(yaml_data)} categories with a total of {sum(len(values) for values in yaml_data.values())} values")

        return yaml_data
    except FileNotFoundError:
        print(f"File {xlsx_file_path} not found")
        return None
    except Exception as e:
        print(e)
        return None

def main():
    xlsx_file_path = "../../因子列表.xlsx"
    sheet_name = "因子梳理"
    category_column = "主题"
    value_column = "信号代码"
    output_yaml_path = "../../configs/field_mappings/factor_mapping.yaml"
    translation_dict = {
        '成长': 'growth',
        '分析师': 'analyst',        '价值': 'value',
        '另类': 'others',
        '情绪': 'sentiment',
        '质量': 'quality'
    }
    _ = xlsx_to_yaml(xlsx_file_path, sheet_name, category_column, value_column, output_yaml_path, translation_dict)


if __name__ == "__main__":
    main()