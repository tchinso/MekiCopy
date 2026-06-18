"""Project-local startup hook.

Runtime compatibility patches live in mekicopy.py so unrelated executables do
not import OCR/GPU packages at startup.
"""
