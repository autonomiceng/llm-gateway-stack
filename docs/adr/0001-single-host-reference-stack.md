---
status: accepted
---

# Define a single-host reference stack

This project is a production-capable single-host reference stack: it provides secure
defaults, durable local data, and tested backup, restore, and upgrade procedures while
allowing planned maintenance downtime. It deliberately does not promise high
availability, zero-downtime upgrades, automated failover, or SLA-backed support; making
those promises would require a materially different architecture and maintenance model.
