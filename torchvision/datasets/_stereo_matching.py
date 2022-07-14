import functools
import json
import os
import random
import shutil
import warnings
from abc import ABC, abstractmethod
from glob import glob
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from PIL import Image

from .utils import download_and_extract_archive, verify_str_arg, _read_pfm
from .vision import VisionDataset

__all__ = (
    "CREStereo"
    "StereoMiddlebury2014"
    "StereoETH3D"
    "StereoKitti2012"
    "StereoKitti2015"
    "StereoSintel"
    "StereoSceneFlow"
    "StereoFallingThings"
    "InStereo2k"
)

_read_pfm_file = functools.partial(_read_pfm, slice_channels=1)


class StereoMatchingDataset(ABC, VisionDataset):
    """Base interface for Stereo matching datasets"""

    def __init__(self, root: str, transforms: Optional[Callable] = None):
        """

        Args:
            root(str): Root directory of the dataset.
            transforms(callable, optional): A function/transform that takes in Tuples of
                (images, disparities, valid_masks) and returns a transformed version of each of them.
                images is a Tuple of (``PIL.Image``, ``PIL.Image``)
                disparities is a Tuple of (``np.ndarray``, ``np.ndarray``) with shape (1, H, W)
                valid_masks is a Tuple of (``np.ndarray``, ``np.ndarray``) with shape (H, W)

                In some cases, when a dataset does not provide disparties, the ``disparities`` and
                ``valid_masks`` can be Tuples containing None values.

                For training splits generally the datasets provide a minimal guarantee of
                images: (``PIL.Image``, ``PIL.Image``)
                disparities: (``np.ndarray``, ``None``) with shape (1, H, W)
                valid_masks: (``np.ndarray``, ``None``) with shape (H, W)

                For some test splits, the datasets provides outputs that look like:
                imgaes: (``PIL.Image``, ``PIL.Image``)
                disparities: (``None``, ``None``)
                valid_masks: (``None``, ``None``)
        """
        super().__init__(root=root)
        self.transforms = transforms

        self._images: List[Tuple] = []
        self._disparities: List[Tuple] = []

    def _read_img(self, file_path: str) -> Image.Image:
        img = Image.open(file_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img

    def _scan_pairs(self, left_pattern: str, right_pattern: str, fill_empty: bool = False) -> List[Tuple[str, str]]:
        left_paths = sorted(glob(left_pattern))
        right_paths = sorted(glob(right_pattern))

        # used when dealing with inexistent disparity for the right image
        if fill_empty:
            right_paths = list("" for _ in left_paths)

        if not left_paths:
            raise FileNotFoundError(f"Could not find any files matching the patterns: {left_pattern}")

        if not right_paths:
            raise FileNotFoundError(f"Could not find any files matching the patterns: {right_pattern}")

        if len(left_paths) != len(right_paths):
            raise ValueError(
                f"Found {len(left_paths)} left files but {len(right_paths)} right files using:\n "
                f"left pattern: {left_pattern}\n"
                f"right pattern: {right_pattern}\n"
            )

        images = list((left, right) for left, right in zip(left_paths, right_paths))
        return images

    @abstractmethod
    def _read_disparity(self, file_path: str) -> Tuple:
        # function that returns a disparity map and an occlusion map
        pass

    def __getitem__(self, index: int) -> Tuple:
        """Return example at given index.

        Args:
            index(int): The index of the example to retrieve

        Returns:
            tuple: A 4-tuple with ``(img_left, img_right, disparity, valid_mask)`` where ``valid_mask``
                is a numpy boolean mask of shape (H, W)
                indicating which disparity values are valid. The disparity is a numpy array of
                shape (1, H, W) and the images are PIL images. ``disparity`` and ``valid_mask`` are None for
                datasets on which for ``split="test"`` the authors did not provide annotations.
        """
        img_left = self._read_img(self._images[index][0])
        img_right = self._read_img(self._images[index][1])

        dsp_map_left, valid_mask_left = self._read_disparity(self._disparities[index][0])
        dsp_map_right, valid_mask_right = self._read_disparity(self._disparities[index][1])

        imgs = (img_left, img_right)
        dsp_maps = (dsp_map_left, dsp_map_right)
        valid_masks = (valid_mask_left, valid_mask_right)

        if self.transforms is not None:
            (
                imgs,
                dsp_maps,
                valid_masks,
            ) = self.transforms(imgs, dsp_maps, valid_masks)

        return imgs[0], imgs[1], dsp_maps[0], valid_masks[0]

    def __len__(self) -> int:
        return len(self._images)


class CREStereo(StereoMatchingDataset):
    """Synthetic dataset used in training the `CREStereo <https://arxiv.org/pdf/2203.11483.pdf>`_ architecture.

    Dataset details on the official paper `repo <https://github.com/megvii-research/CREStereo>`_.

    The dataset is expected to have the following structure: ::

        root
            CREStereo
                tree
                    img1_left.jpg
                    img1_right.jpg
                    img1_left.disp.jpg
                    img1_right.disp.jpg
                    img2_left.jpg
                    img2_right.jpg
                    img2_left.disp.jpg
                    img2_right.disp.jpg
                    ...
                shapenet
                    img1_left.jpg
                    img1_right.jpg
                    img1_left.disp.jpg
                    img1_right.disp.jpg
                    ...
                reflective
                    img1_left.jpg
                    img1_right.jpg
                    img1_left.disp.jpg
                    img1_right.disp.jpg
                    ...
                hole
                    img1_left.jpg
                    img1_right.jpg
                    img1_left.disp.jpg
                    img1_right.disp.jpg
                    ...

    Args:
        root (str): Root directory of the dataset.
        split (str): The split of the dataset to use. One of ``"tree"``, ``"shapenet"``, ``"reflective"``, ``"hole"``
            or ``"all"``. The ``"all"`` split contains all of the above splits.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
        download (bool, optional): If true, downloads the dataset from the internet and puts it in the root directory.
        max_disparity (int, optional): Maximum disparity value. Used to compute the valid mask.
    """

    DOWNLOAD_SPACE = 400 * 1024 * 1024 * 1024

    def __init__(
        self,
        root: str,
        split: str = "tree",
        transforms: Optional[Callable] = None,
        download: bool = False,
        max_disparity: float = 256.0,
    ):
        super().__init__(root, transforms)

        root = Path(root) / "CREStereo"
        self.max_disparity = max_disparity

        # if the API user requests a dataset download check that the user can download it
        if download:
            statvfs = os.statvfs(root)
            # measured in bytes
            available_space = statvfs.f_frsize * statvfs.f_bavail
            if available_space - self.DOWNLOAD_SPACE < 0:
                raise ValueError(
                    f"The storage device for {str(root)} is too small to download the dataset), "
                    f"an additional {self.DOWNLOAD_SPACE - available_space:.2f} GB are required."
                )
            self._download_dataset(str(root))

        verify_str_arg(split, "split", valid_values=("tree", "shapenet", "reflective", "hole", "all"))

        splits = {
            "tree": ["tree"],
            "shapenet": ["shapenet"],
            "reflective": ["reflective"],
            "hole": ["hole"],
            "all": ["hole", "shapenet", "reflective", "hole"],
        }[split]

        for s in splits:
            left_image_pattern = str(root / s / "*_left.jpg")
            right_image_pattern = str(root / s / "*_right.jpg")
            imgs = self._scan_pairs(left_image_pattern, right_image_pattern)
            self._images += imgs

            left_disparity_pattern = str(root / s / "*_left.disp.jpg")
            right_disparity_pattern = str(root / s / "*_right.disp.jpg")
            disparities = self._scan_pairs(left_disparity_pattern, right_disparity_pattern)
            self._disparities += disparities

    def _read_disparity(self, file_path: str) -> Tuple:
        disparity = np.array(Image.open(file_path), dtype=np.float32)
        valid = (disparity < self.max_disparity) & (disparity > 0.0)
        # unsqueeze the disparity map into (C, H, W) format
        disparity = disparity[None, :, :]
        return disparity, valid

    def _download_dataset(self, root: str) -> None:
        dirs = ["tree", "shapenet", "reflective", "hole"]
        # create directory subtree for the download
        for d in dirs:
            d_path = os.path.join(root, d)
            if not os.path.exists(d_path):
                os.makedirs(d_path)

            for i in range(10):
                url = f"https://data.megengine.org.cn/research/crestereo/dataset/{d}/{i}.tar"
                download_and_extract_archive(url=url, download_root=d_path, remove_finished=True)


class StereoMiddlebury2014(StereoMatchingDataset):
    """Publicly available scenes from the Middlebury dataset `2014 version <https://vision.middlebury.edu/stereo/data/scenes2014/>`.

    The dataset mostly follows the original format, without containing the ambient subdirectories.  : ::

        root
            Middlebury2014
                train
                    scene1-{ ,perfect,imperfect}
                        calib.txt
                        im{0,1}.png
                        im1E.png
                        im1L.png
                        disp{0,1}.pfm
                        disp{0,1}-n.png
                        disp{0,1}-sd.pfm
                        disp{0,1}y.pfm
                    scene2-{ ,perfect,imperfect}
                        calib.txt
                        im{0,1}.png
                        im1E.png
                        im1L.png
                        disp{0,1}.pfm
                        disp{0,1}-n.png
                        disp{0,1}-sd.pfm
                        disp{0,1}y.pfm
                    ...
                additional
                    scene1-{ ,perfect,imperfect}
                        calib.txt
                        im{0,1}.png
                        im1E.png
                        im1L.png
                        disp{0,1}.pfm
                        disp{0,1}-n.png
                        disp{0,1}-sd.pfm
                        disp{0,1}y.pfm
                    ...
                test
                    scene1
                        calib.txt
                        im{0,1}.png
                    scene2
                        calib.txt
                        im{0,1}.png
                    ...


    Args:
        root (string): Root directory of the Middleburry 2014 Dataset.
        split (string, optional): The dataset split of scenes, either "train" (default), "test", or "additional"
        use_ambient_views (boolean, optional): Whether to use different expose or lightning views when possible.
            The dataset samples with equal probability between ``[im1.png, im1E.png, im1L.png]``.
        calibration (string, optional): Wether or not to use the calibrated (default) or uncalibrated scenes.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
        download (boolean, optional): Wether or not to download the dataset in the ``root`` directory.
    """

    splits = {
        "train": [
            "Adirondack",
            "Jadeplant",
            "Motorcycle",
            "Piano",
            "Pipes",
            "Playroom",
            "Playtable",
            "Recycle",
            "Shelves",
            "Vintage",
        ],
        "additional": [
            "Backpack",
            "Bicycle1",
            "Cable",
            "Classroom1",
            "Couch",
            "Flowers",
            "Mask",
            "Shopvac",
            "Sticks",
            "Storage",
            "Sword1",
            "Sword2",
            "Umbrella",
        ],
        "test": [
            "Plants",
            "Classroom2E",
            "Classroom2",
            "Australia",
            "DjembeL",
            "CrusadeP",
            "Crusade",
            "Hoops",
            "Bicycle2",
            "Staircase",
            "Newkuba",
            "AustraliaP",
            "Djembe",
            "Livingroom",
            "Computer",
        ],
    }

    def __init__(
        self,
        root: str,
        split: str = "train",
        calibration: Optional[str] = "perfect",
        use_ambient_views: bool = False,
        transforms: Optional[Callable] = None,
        download: bool = False,
    ):
        super().__init__(root, transforms)

        verify_str_arg(split, "split", valid_values=("train", "test", "additional"))

        if calibration:
            verify_str_arg(calibration, "calibration", valid_values=("perfect", "imperfect", "both"))  # type: ignore
            if split == "test":
                calibration = None
                warnings.warn(
                    "\nSplit 'test' has only no calibration settings, ignoring calibration argument.", RuntimeWarning
                )
        else:
            if split != "test":
                calibration = "perfect"
                warnings.warn(
                    f"\nSplit '{split}' has calibration settings, however None was provided as an argument."
                    f"\nSetting calibration to 'perfect' for split '{split}'. Available calibration settings are: 'perfect', 'imperfect', 'both'.",
                    RuntimeWarning,
                )

        if download:
            self._download_dataset(root)

        root = Path(root) / "Middlebury2014"

        if not os.path.exists(root / split):
            raise FileNotFoundError(f"The {split} directory was not found in the provided root directory")

        split_scenes = self.splits[split]
        # check that the provided root folder contains the scene splits
        if not any(
            # using startswith to account for perfect / imperfect calibrartion
            scene.startswith(s)
            for scene in os.listdir(root / split)
            for s in split_scenes
        ):
            raise FileNotFoundError(f"Provided root folder does not contain any scenes from the {split} split.")

        calibrartion_suffixes = {
            None: [""],
            "perfect": ["-perfect"],
            "imperfect": ["-imperfect"],
            "both": ["-perfect", "-imperfect"],
        }[calibration]

        for calibration_suffix in calibrartion_suffixes:
            scene_pattern = "*" + calibration_suffix
            left_img_pattern = str(root / split / scene_pattern / "im0.png")
            right_img_pattern = str(root / split / scene_pattern / "im1.png")
            self._images += self._scan_pairs(left_img_pattern, right_img_pattern)

            if split == "test":
                self._disparities += list(("", "") for _ in self._images)
            else:
                left_dispartity_pattern = str(root / split / "*" / "disp0.pfm")
                right_dispartity_pattern = str(root / split / "*" / "disp1.pfm")
                self._disparities += self._scan_pairs(left_dispartity_pattern, right_dispartity_pattern)

        self.use_ambient_views = use_ambient_views

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)

    def _read_img(self, file_path: str) -> Image.Image:
        """Function that reads either the original right image or an augmented view when ``use_ambient_views`` is True."""
        if os.path.basename(file_path) == "im1.png" and self.use_ambient_views:
            # initialize sampleable container
            base_path = os.path.basename(file_path)[0]
            ambient_file_paths = list(os.path.join(base_path, view_name) for view_name in ["im1E.png", "im1L.png"])
            # double check that we're not going to try to read from an invalid file path
            ambient_file_paths = list(filter(lambda p: os.path.exists(p), ambient_file_paths))
            # keep the original image as an option as well for uniform sampling between base views
            ambient_file_paths.append(file_path)
            file_path = random.choice(ambient_file_paths)
        return super()._read_img(file_path)

    def _read_disparity(self, file_path: str) -> Tuple:
        if not os.path.exists(file_path):  # case when dealing with the test split
            return None, None
        disparity_map = _read_pfm_file(file_path)
        valid_mask = disparity_map < 1e3
        # remove the channel dimension from the valid mask
        valid_mask = valid_mask[0, :, :]
        return disparity_map, valid_mask

    def _download_dataset(self, root: str):
        base_url = "https://vision.middlebury.edu/stereo/data/scenes2014/zip"
        # train and additional splits have 2 different calibration settings
        root = Path(root) / "Middlebury2014"
        for split_name, split_scenes in self.splits.items():
            if split_name == "test":
                continue
            split_root = root / split_name
            for scene in split_scenes:
                for calibration in ["perfect", "imperfect"]:
                    scene_name = f"{scene}-{calibration}"
                    scene_url = f"{base_url}/{scene_name}.zip"
                    download_and_extract_archive(
                        url=scene_url, filename=f"{scene_name}.zip", download_root=str(split_root), remove_finished=True
                    )

        if any(s not in os.listdir(root) for s in self.splits["test"]):
            # test split is downloaded from a different location
            test_set_url = "https://vision.middlebury.edu/stereo/submit3/zip/MiddEval3-data-F.zip"

            # the unzip is going to produce a directory MiddEval3 with two subdirectories trainingF and testF
            # we want to move the contents from testF into the  directory
            download_and_extract_archive(url=test_set_url, download_root=str(root), remove_finished=True)
            for scene_dir, scene_names, _ in os.walk(str(root / "MiddEval3/testF")):
                for scene in scene_names:
                    scene_dst_dir = root / "test" / scene
                    scene_src_dir = Path(scene_dir) / scene
                    os.makedirs(scene_dst_dir, exist_ok=True)
                    shutil.move(str(scene_src_dir), str(scene_dst_dir))

            # cleanup MiddEval3 directory
            shutil.rmtree(str(root / "MiddEval3"))


class StereoETH3D(StereoMatchingDataset):
    """ "ETH3D `Low-Res Two-View <https://www.eth3d.net/datasets>`_ dataset.

    The dataset is expected to have the following structure: ::

        root
            ETH3D
                two_view_training
                    scene1
                        im1.png
                        im0.png
                        images.txt
                        cameras.txt
                        calib.txt
                    scene2
                        im1.png
                        im0.png
                        images.txt
                        cameras.txt
                        calib.txt
                    ...
                two_view_training_gt
                    scene1
                        disp0GT.pfm
                        mask0nocc.png
                    scene2
                        disp0GT.pfm
                        mask0nocc.png
                    ...
                two_view_testing
                    scene1
                        im1.png
                        im0.png
                        images.txt
                        cameras.txt
                        calib.txt
                    scene2
                        im1.png
                        im0.png
                        images.txt
                        cameras.txt
                        calib.txt
                    ...

    Args:
        root (string): Root directory of the ETH3D Dataset.
        split (string, optional): The dataset split of scenes, either "train" (default) or "test".
        calibration (string, optional): Wether or not to use the calibrated (default) or uncalibrated scenes.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
    """

    def __init__(self, root: str, split: str = "train", transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        verify_str_arg(split, "split", valid_values=("train", "test"))

        root = Path(root) / "ETH3D"

        img_dir = "two_view_training" if split == "train" else "two_view_test"
        anot_dir = "two_view_training_gt"

        left_img_pattern = str(root / img_dir / "*" / "im0.png")
        right_img_pattern = str(root / img_dir / "*" / "im1.png")
        self._images = self._scan_pairs(left_img_pattern, right_img_pattern)

        if split == "test":
            self._disparities = list(("", "") for _ in self._images)
        else:
            disparity_pattern = str(root / anot_dir / "*" / "disp0GT.pfm")
            self._disparities = self._scan_pairs(disparity_pattern, "", fill_empty=True)

    def _read_disparity(self, file_path: str) -> Tuple:
        if not os.path.exists(file_path):
            return None, None

        disparity_map = _read_pfm_file(file_path)
        mask_path = os.path.join(os.path.split(file_path)[0], "mask0nocc.png")
        valid_mask = Image.open(mask_path)
        valid_mask = np.array(valid_mask).astype(np.bool_)
        return disparity_map, valid_mask

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class StereoKitti2012(StereoMatchingDataset):
    """ "Kitti dataset from the `2012 <http://www.cvlibs.net/datasets/kitti/eval_stereo_flow.php>`_ stereo evaluation benchmark.
    Uses the RGB images for consistency with Kitti 2015.

    The dataset is expected to have the following structure: ::

        root
            Kitti2012
                testing
                    colored_0
                    colored_1
                training
                    colored_0
                    colored_1
                    disp_noc
                    calib

    Args:
        root (string): Root directory where Kitti2012 is located.
        split (string, optional): The dataset split of scenes, either "train" (default), test, or "additional"
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
        download (boolean, optional): Wether or not to download the dataset in the ``root`` directory.
    """

    def __init__(self, root: str, split: str = "train", transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        verify_str_arg(split, "split", valid_values=("train", "test"))

        root = Path(root) / "Kitti2012" / (split + "ing")

        left_img_pattern = str(root / "colored_0" / "*_10.png")
        right_img_pattern = str(root / "colored_1" / "*_10.png")
        self._images = self._scan_pairs(left_img_pattern, right_img_pattern)

        if split == "train":
            disparity_pattern = str(root / "disp_noc" / "*.png")
            self._disparities = self._scan_pairs(disparity_pattern, "", fill_empty=True)
        else:
            self._disparities = list(("", "") for _ in self._images)

    def _read_disparity(self, file_path: str) -> Tuple:
        if not os.path.exists(file_path):
            return None, None

        disparity_map = np.array(Image.open(file_path)) / 256.0
        valid_mask = disparity_map > 0.0
        # unsqueeze the disparity map into (C, H, W) format
        disparity_map = disparity_map[None, :, :]
        return disparity_map, valid_mask

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class StereoKitti2015(StereoMatchingDataset):
    """ "Kitti dataset from the `2015 <http://www.cvlibs.net/datasets/kitti/eval_scene_flow.php>`_ stereo evaluation benchmark.

    The dataset is expected to have the following structure: ::

        root
            Kitti2015
                testing
                    image_2
                        img1.png
                        img2.png
                        ...
                    image_3
                        img1.png
                        img2.png
                        ...
                training
                    image_2
                        img1.png
                        img2.png
                        ...
                    image_3
                        img1.png
                        img2.png
                        ...
                    disp_occ_0
                        img1.png
                        img2.png
                        ...
                    disp_occ_1
                        img1.png
                        img2.png
                        ...
                    calib

    Args:
        root (string): Root directory where Kitti2015 is located.
        split (string, optional): The dataset split of scenes, either "train" (default) or test.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
    """

    def __init__(self, root: str, split: str = "train", transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        verify_str_arg(split, "split", valid_values=("train", "test"))

        root = Path(root) / "Kitti2015" / (split + "ing")
        left_img_pattern = str(root / "image_2" / "*.png")
        right_img_pattern = str(root / "image_3" / "*.png")
        self._images = self._scan_pairs(left_img_pattern, right_img_pattern)

        if split == "train":
            left_disparity_pattern = str(root / "disp_occ_0" / "*.png")
            right_disparity_pattern = str(root / "disp_occ_1" / "*.png")
            self._disparities = self._scan_pairs(left_disparity_pattern, right_disparity_pattern)
        else:
            self._disparities = list(("", "") for _ in self._images)

    def _read_disparity(self, file_path: str) -> Tuple:
        if not os.path.exists(file_path):
            return None, None

        disparity_map = np.array(Image.open(file_path)) / 256.0
        valid_mask = disparity_map < 0.0
        # unsqueeze the disparity map into (C, H, W) format
        disparity_map = disparity_map[None, :, :]
        return disparity_map, valid_mask

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class StereoSintel(StereoMatchingDataset):
    """ "Sintel `Stereo Dataset <http://sintel.is.tue.mpg.de/stereo>`_.

    The dataset is expected to have the following structure: ::

        root
            Sintel
                training
                    final_left
                        scene1
                            img1.png
                            img2.png
                            ...
                        ...
                    final_right
                        scene2
                            img1.png
                            img2.png
                            ...
                        ...
                    disparities
                        scene1
                            img1.png
                            img2.png
                            ...
                        ...
                    occlusions
                        scene1
                            img1.png
                            img2.png
                            ...
                        ...
                    outofframe
                        scene1
                            img1.png
                            img2.png
                            ...
                        ...

    Args:
        root (string): Root directory where Sintel Stereo is located.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
    """

    def __init__(self, root: str, transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        root = Path(root) / "Sintel"

        left_img_pattern = str(root / "training" / "final_left" / "*" / "*.png")
        right_img_pattern = str(root / "training" / "final_right" / "*" / "*.png")
        self._images = self._scan_pairs(left_img_pattern, right_img_pattern)

        disparity_pattern = str(root / "training" / "disparities" / "*" / "*.png")
        self._disparities = self._scan_pairs(disparity_pattern, "", fill_empty=True)

    def _get_oclussion_mask_paths(self, file_path: str) -> List[str]:
        path_tokens = file_path.split(os.sep)
        for idx in range(len(path_tokens) - 1):
            if path_tokens[idx] == "training" and path_tokens[idx + 1] == "disparities":
                pre_tokens = path_tokens[: idx + 1]
                post_tokens = path_tokens[idx + 2 :]
                return (
                    "/".join(pre_tokens + ["occlusions"] + post_tokens),
                    "/".join(pre_tokens + ["outofframe"] + post_tokens),
                )

    def _read_disparity(self, file_path: str) -> Tuple:
        if not os.path.exists(file_path):
            return None, None

        # disparity decoding as per Sintel instructions
        disparity_map = np.array(Image.open(file_path), dtype=np.float32)
        r, g, b = np.split(disparity_map, 3, axis=-1)
        disparity_map = r * 4 + g / (2 ** 6) + b / (2 ** 14)
        # reshape into (C, H, W) format
        disparity_map = np.transpose(disparity_map, (2, 0, 1))
        # find the appropiate file paths
        occlued_mask_path, out_of_frame_mask_path = self._get_oclussion_mask_paths(file_path)
        # occlusion masks
        valid_mask = np.array(Image.open(occlued_mask_path)) == 0
        # out of frame masks
        off_mask = np.array(Image.open(out_of_frame_mask_path)) == 0
        # combine the masks together
        valid_mask = np.logical_and(off_mask, valid_mask)
        return disparity_map, valid_mask

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class StereoSceneFlow(StereoMatchingDataset):
    """Dataset interface for `Scene Flow <https://lmb.informatik.uni-freiburg.de/resources/datasets/SceneFlowDatasets.en.html>`_ datasets.

    The dataset is expected to have the following structre: ::

        root
            SceneFlow
                Monkaa
                    frames_cleanpass
                        scene1
                            left
                                img1.png
                                img2.png
                            right
                                img1.png
                                img2.png
                        scene2
                            left
                                img1.png
                                img2.png
                            right
                                img1.png
                                img2.png
                    frames_finalpass
                        scene1
                            left
                                img1.png
                                img2.png
                            right
                                img1.png
                                img2.png
                        ...
                        ...
                    disparity
                        scene1
                            left
                                img1.pfm
                                img2.pfm
                            right
                                img1.pfm
                                img2.pfm
                FlyingThings3D
                    ...
                    ...

    Args:
        root (string): Root directory where SceneFlow is located.
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
    """

    def __init__(
        self, root: str, split: str = "FlyingThings3D", pass_name: str = "clean", transforms: Optional[Callable] = None
    ):
        super().__init__(root, transforms)

        root = Path(root) / "SceneFlow"

        verify_str_arg(split, "split", valid_values=("FlyingThings3D", "Driving", "Monkaa"))
        verify_str_arg(pass_name, "pass_name", valid_values=("clean", "final", "both"))

        passes = {
            "clean": ["frames_cleanpass"],
            "final": ["frames_finalpass"],
            "both": ["frames_cleanpass, frames_finalpass"],
        }[pass_name]

        root = root / split

        for p in passes:
            left_img_pattern = str(root / p / "*" / "left" / "*.png")
            right_img_pattern = str(root / p / "*" / "right" / "*.png")
            self._images += self._scan_pairs(left_img_pattern, right_img_pattern)

            left_disparity_pattern = str(root / "disparity" / "*" / "left" / "*.pfm")
            right_disparity_pattern = str(root / "disparity" / "*" / "right" / "*.pfm")
            self._disparities += self._scan_pairs(left_disparity_pattern, right_disparity_pattern)

    def _read_disparity(self, file_path: str) -> Tuple:
        disparity = _read_pfm_file(file_path)
        # keep valid mask with shape (H, W)
        valid = np.ones(disparity.shape[1:]).astype(np.bool_)
        return disparity, valid

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class StereoFallingThings(StereoMatchingDataset):
    """FallingThings `<https://research.nvidia.com/publication/2018-06_falling-things-synthetic-dataset-3d-object-detection-and-pose-estimation>`_ dataset

    The dataset is expected to have the following structre: ::

        root
            FallingThings
                single
                    scene1
                        _object_settings.json
                        _camera_settings.json
                        image1.left.depth.png
                        image1.right.depth.png
                        image1.left.jpg
                        image1.right.jpg
                        image2.left.depth.png
                        image2.right.depth.png
                        image2.left.jpg
                        image2.right
                        ...
                    scene2
                    ...
                mixed
                    scene1
                        _object_settings.json
                        _camera_settings.json
                        image1.left.depth.png
                        image1.right.depth.png
                        image1.left.jpg
                        image1.right.jpg
                        image2.left.depth.png
                        image2.right.depth.png
                        image2.left.jpg
                        image2.right
                        ...
                    scene2
                    ...

    Args:
        root (string): Root directory where FallingThings is located.
        split (string): Either "single", "mixed", or "both".
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.

    """

    def __init__(self, root: str, split: str = "single", transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        root = Path(root) / "FallingThings"

        verify_str_arg(split, "split", valid_values=("single", "mixed", "both"))

        splits = {
            "single": ["single"],
            "mixed": ["mixed"],
            "both": ["single", "mixed"],
        }[split]

        for s in splits:
            left_img_pattern = str(root / s / "*" / "*.left.jpg")
            right_img_pattern = str(root / s / "*" / "*.right.jpg")
            self._images += self._scan_pairs(left_img_pattern, right_img_pattern)

            left_disparity_pattern = str(root / s / "*" / "*.left.depth.png")
            right_disparity_pattern = str(root / s / "*" / "*.right.depth.png")
            self._disparities += self._scan_pairs(left_disparity_pattern, right_disparity_pattern)

    def _read_disparity(self, file_path: str) -> Tuple:
        # (H, W) image
        depth = np.array(Image.open(file_path))
        # as per https://research.nvidia.com/sites/default/files/pubs/2018-06_Falling-Things/readme_0.txt
        # in order to extract disparity from depth maps
        with open(os.path.split(file_path)[0] + "/_camera_settings.json", "r") as f:
            intrinsics = json.load(f)
            fx = intrinsics["camera_settings"][0]["intrinsic_settings"]["fx"]
            # inverse of depth-from-disparity equation
            disparity = (fx * 6.0 * 100) / depth.astype(np.float32)
            valid = disparity > 0
            # unsqueeze disparity to (C, H, W)
            disparity = disparity[None, :, :]
            return disparity, valid

    def __getitem__(self, index: int) -> Tuple:
        return super().__getitem__(index)


class InStereo2k(StereoMatchingDataset):
    """InStereo2k `<https://github.com/YuhuaXu/StereoDataset>`_ dataset

    The dataset is expected to have the following structre: ::

        root
            InStereo2k
                train
                    scene1
                        left.png
                        right.png
                        left_disp.png
                        right_disp.png
                        ...
                    scene2
                    ...
                test
                    scene1
                        left.png
                        right.png
                        left_disp.png
                        right_disp.png
                        ...
                    scene2
                    ...

    Args:
        root (string): Root directory where InStereo2k is located.
        split (string): Either "train" or "test".
        transforms (callable, optional): A function/transform that takes in a sample and returns a transformed version.
    """

    def __init__(self, root: str, split: str = "train", transforms: Optional[Callable] = None):
        super().__init__(root, transforms)

        root = Path(root) / "InStereo2k" / split

        verify_str_arg(split, "split", valid_values=("train", "test"))

        left_img_pattern = str(root / "*" / "left.png")
        right_img_pattern = str(root / "*" / "right.png")
        self._images = self._scan_pairs(left_img_pattern, right_img_pattern)

        left_disparity_pattern = str(root / "*" / "left_disp.png")
        right_disparity_pattern = str(root / "*" / "right_disp.png")
        self._disparities = self._scan_pairs(left_disparity_pattern, right_disparity_pattern)

    def _read_disparity(self, file_path: str) -> Tuple:
        disparity = np.array(Image.open(file_path), dtype=np.float32)
        valid = np.ones_like(disparity).astype(np.bool_)
        # unsqueeze disparity to (C, H, W)
        disparity = disparity[None, :, :]
        return disparity, valid