from setuptools import setup, find_packages

setup(
    name="distributed-videoconf",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "pyzmq>=25.0.0",
        "opencv-python>=4.8.0",
        "pyaudio>=0.2.13",
        "numpy>=1.24.0",
        "msgpack>=1.0.5",
        "pyyaml>=6.0",
    ],
    python_requires=">=3.8",
)
