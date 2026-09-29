from setuptools import setup, find_packages

install_requires = [
    "tqdm",
    "termcolor",
    "PyMuPDF>=1.23.0",  # 用于解析 PDF (import pymupdf)
    "urllib3>=1.26"
]

setup(
    name="autocheck",
    install_requires=install_requires,
    version="0.1.0",
    python_requires=">=3.8",
    packages=find_packages(include=["autocheck*"]),
    entry_points={
        'console_scripts': [
            "autocheck=autocheck.cli:main",
        ],
    },
)
