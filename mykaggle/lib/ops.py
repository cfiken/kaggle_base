import numpy as np


def sigmoid(a):
    return 1 / (1 + np.exp(-a))


def softmax(x: np.ndarray) -> np.ndarray:
    x_max = np.max(x, axis=-1, keepdims=True)
    a = np.exp(x - x_max)
    a = a / np.sum(a, axis=-1, keepdims=True)
    return a
