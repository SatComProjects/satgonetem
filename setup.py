from setuptools import setup, find_packages

setup(
    name="satgonetem-core",
    version="0.1.0",
    description="SatGoNetem Core - Satellite Network Emulation Tool",
    long_description=(
        open("README.md").read() if __import__("os").path.exists("README.md") else ""
    ),
    long_description_content_type="text/markdown",
    author="Juan Arias Suarez",
    author_email="juan.arias-suarez@isae-supaero.fr",
    url="https://github.com/jariassuarez/satgonetem",
    packages=find_packages(),
    include_package_data=True,
    python_requires=">=3.8",
    install_requires=[
        "docker>=7.1.0",
        "grpcio>=1.62.0",
        "matplotlib>=3.10.0",
        "networkx>=3.4.0",
        "numpy>=2.2.0",
        "Pillow>=11.2.0",
        "protobuf>=6.31.0",
        "PyYAML>=6.0.0",
        "sgp4>=2.23",
        "uvicorn[standard]>=0.29.0",
        "jinja2>=3.1.0",
        "fastapi>=0.110.0",
        "python-multipart>=0.0.9",
        "psutil>=7.0.0",
        "pandas>=2.3.0",
        "astropy>=6.1.7",
        "sat_com_topology @ git+https://github.com/SatComProjects/satComTopology.git",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Science/Research",
        "Topic :: System :: Networking",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    package_data={
        "satgonetem": [
            "webgui/templates/*.html",
            "webgui/static/*",
        ],
    },
)
