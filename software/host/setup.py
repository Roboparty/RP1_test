"""Compatibility shim for editable installs with older pip frontends."""

from setuptools import find_packages, setup


setup(
    name="rp1-factory-hmi",
    version="0.1.0",
    description="RP1 production aging-test control gateway and desktop HMI",
    packages=find_packages(include=("factory_hmi", "factory_hmi.*", "scripts")),
    package_data={
        "factory_hmi": [
            "config/*.yaml",
            "contracts/*.json",
            "desktop/assets/*.png",
            "desktop/assets/*.qss",
        ],
        "scripts": ["config/*.yaml"],
    },
    python_requires=">=3.10",
    install_requires=[
        "fastapi",
        "numpy",
        "pydantic",
        "pymodbus==3.8.6",
        "pyroute2",
        "PyYAML",
        "uvicorn[standard]",
    ],
    extras_require={
        "desktop": ["PySide6"],
        "build": ["PyInstaller"],
        "test": ["httpx", "pytest", "jsonschema"],
    },
    entry_points={
        "console_scripts": [
            "rp1-factory-gateway=factory_hmi.gateway.__main__:main",
            "rp1-factory-hmi=factory_hmi.desktop.__main__:main",
            "rp1-factory-can-helper=factory_hmi.can_helper:main",
            "rp1-factory-uploader=factory_hmi.uploader.__main__:main",
        ]
    },
)
