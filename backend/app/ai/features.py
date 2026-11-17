"""Extracción de características de imagen para el recomendador.

Dos representaciones (Laboratorio 11 del curso):

- **BoVW clásico**: SIFT (150 keypoints sobre escala de grises 224x224)
  -> asignación de cada descriptor al centroide más cercano del codebook
  (K=100, entrenado con K-Means por ``scripts/prepare_recommender.py``)
  -> tokens ``vw{id}`` repetidos según su frecuencia: el índice invertido
  deriva el TF de las repeticiones, igual que un documento textual.
- **Embeddings densos**: ResNet-18 preentrenada en ImageNet con la capa
  ``fc`` reemplazada por ``Identity`` -> vector 512D L2-normalizado.

Los imports pesados (cv2, torch) son perezosos: ni el motor MiniDB ni la
API básica los necesitan, solo los endpoints del recomendador.
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

import numpy as np

DATA_DIR = os.environ.get("DATA_DIR", "./data")

SIFT_FEATURES = 150
IMAGE_SIZE = (224, 224)


def codebook_path() -> str:
    """Ruta del codebook BoVW (``codebook.npy``, K x 128 float32)."""
    return os.path.join(DATA_DIR, "codebook.npy")


def images_dir() -> str:
    """Directorio de los thumbnails de producto servidos por la API."""
    return os.path.join(DATA_DIR, "app_images")


def config_path() -> str:
    """Ruta de ``app_config.json`` (tabla, dimensión, codebook, etc.)."""
    return os.path.join(DATA_DIR, "app_config.json")


def decode_image(image_bytes: bytes, grayscale: bool = False) -> np.ndarray:
    """Decodifica bytes (JPEG/PNG) a una imagen OpenCV."""
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR
    img = cv2.imdecode(arr, flag)
    if img is None:
        raise ValueError("los bytes no son una imagen decodificable")
    return img


def sift_descriptors(img_gray: np.ndarray) -> np.ndarray:
    """Descriptores SIFT (hasta ``SIFT_FEATURES``) de una imagen gris.

    Devuelve una matriz ``(m, 128)`` float32; ``m = 0`` si la imagen no
    produjo keypoints (queda como documento BoVW vacío).
    """
    import cv2
    img = cv2.resize(img_gray, IMAGE_SIZE)
    sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES)
    _, desc = sift.detectAndCompute(img, None)
    if desc is None:
        return np.empty((0, 128), dtype=np.float32)
    return desc


@lru_cache(maxsize=1)
def load_codebook() -> np.ndarray:
    """Codebook BoVW (centroides K-Means) entrenado en la preparación."""
    return np.load(codebook_path())


def visual_words(descriptors: np.ndarray,
                 codebook: np.ndarray) -> list[str]:
    """Tokens ``vw{id}`` por descriptor (centroide más cercano, L2)."""
    if len(descriptors) == 0:
        return []
    d2 = ((descriptors[:, None, :] - codebook[None, :, :]) ** 2).sum(axis=2)
    assign = d2.argmin(axis=1)
    return [f"vw{w}" for w in assign]


def image_visual_words(image_bytes: bytes) -> list[str]:
    """BoVW de una imagen: SIFT + asignación al codebook cargado."""
    return visual_words(sift_descriptors(decode_image(image_bytes,
                                                      grayscale=True)),
                        load_codebook())


class _ResNetEncoder:
    """Singleton perezoso de ResNet-18 (pesos ImageNet, fc = Identity)."""

    _instance: tuple | None = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            import torch
            from torchvision import models, transforms
            net = models.resnet18(weights=models.ResNet18_Weights.DEFAULT)
            net.fc = torch.nn.Identity()
            net.eval()
            pre = transforms.Compose([
                transforms.Resize(IMAGE_SIZE),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225]),
            ])
            cls._instance = (torch, net, pre)
        return cls._instance


def embed_pil_images(images: list) -> np.ndarray:
    """Embeddings L2-normalizados (512D) de un lote de imágenes PIL."""
    torch, net, pre = _ResNetEncoder.get()
    batch = torch.stack([pre(img) for img in images])
    with torch.no_grad():
        feats = net(batch).numpy()
    norms = np.linalg.norm(feats, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (feats / norms).astype(np.float32)


def image_embedding(image_bytes: bytes) -> list[float]:
    """Embedding 512D de una imagen en bytes (consulta del recomendador)."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return [float(v) for v in embed_pil_images([img])[0]]
