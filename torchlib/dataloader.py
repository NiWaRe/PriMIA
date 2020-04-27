import os
import pandas as pd
from PIL import Image
from torch.utils import data
from torchvision import transforms
from torchvision.datasets.folder import default_loader
import syft as sy


def single_channel_loader(filename):
    with open(filename, "rb") as f:
        img = Image.open(f).convert("L")
        return img.copy()


class PPPP(sy.BaseDataset):
    def __init__(self, label_path="Labels.csv", train=False, transform=None):
        self.class_names = {0: "normal", 1: "bacterial pneumonia", 2: "viral pneumonia"}
        self.train = train
        self.labels = pd.read_csv(label_path)
        self.labels = self.labels[
            self.labels["Dataset_type"] == ("TRAIN" if train else "TEST")
        ]
        self.transform = transform

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        row = self.labels.iloc[index]
        label = row["Numeric_Label"]
        path = "train" if self.train else "test"
        path = os.path.join(path, row["X_ray_image_name"])
        img = single_channel_loader(path)
        if self.transform:
            img = self.transform(img)
        return img, label

    def get_class_name(self, numeric_label):
        return self.class_names[numeric_label]

    def get_class_occurances(self):
        return dict(self.labels["Numeric_Label"].value_counts())


if __name__ == "__main__":
    ds = PPPP(
        transform=transforms.Compose(
            [transforms.Resize(224), transforms.CenterCrop(224)]
        )
    )
    ds.get_class_occurances()
    L = len(ds)
    print("length test set: {:d}".format(L))
    img, label = ds[1]
    # img.show()
    tf = transforms.Compose(
        [transforms.Resize(224), transforms.CenterCrop(224), transforms.ToTensor()]
    )  # TODO: Add normalization
    ds = PPPP(train=True, transform=tf)
    L = len(ds)
    print("length train set: {:d}".format(L))
    img, label = ds[0]
    print(img.size())
    print(label)
    """
    cnt = 0
    import tqdm

    for img, label in tqdm.tqdm(ds, total=L, leave=False):
        if img.size(0) != 1:
            cnt += 1
    print("{:d} images that are not grayscale".format(cnt))"""

    ds = PPPP()
    import matplotlib.pyplot as plt

    hist = ds.labels.hist(bins=3, column="Numeric_Label")
    plt.show()
