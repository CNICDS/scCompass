import os
from abc import ABC, abstractmethod

import pandas as pd


class SexDetermine(ABC):
    def __init__(self, x_gene_list, y_gene_list):
        self.x_gene_list = x_gene_list
        self.y_gene_list = y_gene_list

    def data_process(self, sex_determine_data):
        x_gene_sum = sex_determine_data.loc[:, sex_determine_data.columns == "Xist"].sum(axis=1)
        total_sum = sex_determine_data.sum(axis=1)
        x_ratio = x_gene_sum / total_sum

        a = sex_determine_data.loc[:, sex_determine_data.columns.isin(self.x_gene_list["Gene name"])]
        b = sex_determine_data.loc[:, sex_determine_data.columns.isin(self.y_gene_list["Gene name"])]
        c = b.loc[:, ~b.columns.isin(a.columns)]
        y_gene_sum = sex_determine_data.loc[:, sex_determine_data.columns.isin(c.columns)].sum(axis=1)
        total_sum1 = sex_determine_data.sum(axis=1)
        y_ratio = y_gene_sum / total_sum1

        sample_y_gene = pd.DataFrame(y_ratio, columns=['ratioY'])
        sample_x_gene = pd.DataFrame(x_ratio, columns=['ratioX'])

        return sample_x_gene, sample_y_gene

    def save(self, path, data):
        data.to_csv(path, index=False)

    @abstractmethod
    def determine(self, sex_determine_data):
        pass


class HumanSexDetermine(SexDetermine):
    def __init__(self, x_gene_list=None, y_gene_list=None):
        if x_gene_list is None:
            x_gene_list = pd.read_excel(
                os.path.join(os.getcwd(), "gene_data", "sex_determine", "Homo_sapiens_chrX.xlsx"))
        if y_gene_list is None:
            y_gene_list = pd.read_csv(
                os.path.join(os.getcwd(), "gene_data", "sex_determine", "human_Ygenelist.txt"), sep=",")
        super().__init__(x_gene_list, y_gene_list)

    def determine(self, sex_determine_data):
        sample_x_gene, sample_y_gene = self.data_process(sex_determine_data)
        gender_determine_result = pd.DataFrame(columns=["Sample ID", "Gender"])

        for idx in range(sample_x_gene.shape[0]):
            ratio_x = sample_x_gene.iloc[idx]['ratioX']
            ratio_y = sample_y_gene.iloc[idx]['ratioY']
            if ratio_y >= 0.001188:
                gender = "Male"
            elif ratio_x <= 0.000070 and ratio_y > 0.000106:
                gender = "Male"
            elif ratio_x >= 0.000001 and ratio_y <= 0.000106:
                gender = "Female"
            elif ratio_x > 0.000070 and 0.000106 < ratio_y <= 0.001188:
                gender = "Mix"
            else:
                gender = "unknown"

            # gender_determine_result.append(gender)
            gender_determine_result.loc[gender_determine_result.shape[0]] = [sex_determine_data.index[idx], gender]

        return gender_determine_result


class MouseSexDetermine(SexDetermine):
    def __init__(self, x_gene_list=None, y_gene_list=None):
        if x_gene_list is None:
            x_gene_list = pd.read_excel(
                os.path.join(os.getcwd(), "gene_data", "sex_determine", "Mus_musculus_chrX.xlsx"))
        if y_gene_list is None:
            y_gene_list = pd.read_csv(
                os.path.join(os.getcwd(), "gene_data", "sex_determine", "mouse_Ygenelist.txt"), sep=",")
        super().__init__(x_gene_list, y_gene_list)

    def determine(self, sex_determine_data):
        sample_x_gene, sample_y_gene = self.data_process(sex_determine_data)
        gender_determine_result = pd.DataFrame(columns=["Sample ID", "Gender"])

        for idx in range(sample_x_gene.shape[0]):
            ratio_x = sample_x_gene.iloc[idx]['ratioX']
            ratio_y = sample_y_gene.iloc[idx]['ratioY']
            if ratio_y >= 0.000065:
                gender = "Male"
            elif ratio_x <= 0.000008 and 0.000004 < ratio_y < 0.000065:
                gender = "Male"
            elif ratio_x >= 0.000555 and 0.000004 < ratio_y < 0.000065:
                gender = "Female"
            elif ratio_x > 0 and ratio_y <= 0.000004:
                gender = "Female"
            elif 0.000008 < ratio_x < 0.000555 and 0.000004 < ratio_y < 0.000065:
                gender = "Mix"
            else:
                gender = "unknown"

            # gender_determine_result.append(gender)
            gender_determine_result.loc[gender_determine_result.shape[0]] = [sex_determine_data.index[idx], gender]

        return gender_determine_result
