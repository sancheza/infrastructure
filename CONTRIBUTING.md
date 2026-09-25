# Contributing to Infrastructure & Network Utilities

Thank you for your interest in contributing! This project is a collection of useful infrastructure scripts, and we welcome improvements, bug fixes, and new tools.

## How to Contribute

1.  **Fork the repository** to your own GitHub account.
2.  **Clone the fork** to your local machine.
3.  **Create a new branch** for your changes:
    ```bash
    git checkout -b feature/my-new-tool
    ```
4.  **Make your changes**. Ensure your code is well-commented and follows the established style of other scripts in the repo.
5.  **Test your changes**. Run the scripts in a safe environment to ensure they work as expected.
6.  **Commit your changes** with a clear and descriptive commit message:
    ```bash
    git commit -m "Add network latency monitor for Linux"
    ```
7.  **Push to your fork**:
    ```bash
    git push origin feature/my-new-tool
    ```
8.  **Submit a Pull Request** (PR) to the `main` branch of this repository.

## Coding Standards

- **Python**: Follow dev ADR 0004 (Google Python Style Guide baseline) and dev ADR 0005 (command-line conventions). Lint with `pylint`.
- **Shell**: Follow dev ADR 0006 and verify scripts with `shellcheck`.
- **Documentation**: New scripts must be added to the `README.md` with a description and usage examples.
- **ADRs**: The dev-level ADRs are in the [engineering-standards repository](https://github.com/sancheza/engineering-standards) (`adr/`).

## Code of Conduct

Please be respectful and professional in all communications. We aim to keep this a helpful and welcoming project for everyone.
