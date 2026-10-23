"""The Hermes commit that the compatibility CI installs.

Coconut runs on the Hermes that the person installed. Nothing in Coconut installs this commit. The native-kanban
job of .github/workflows/hermes-compatibility.yml reads REVISION until that workflow tests a range of Hermes versions.
"""

REVISION = "4d04303818b61bf3aa09278af48356315515965b"
