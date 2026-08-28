needquery vs. needtable
=======================

A tiny graph: three software requirements, one of them (SWREQ_C) verified by no
test. First the data:

.. swreq:: Lane detection
   :id: SWREQ_A

.. swreq:: Deviation warning
   :id: SWREQ_B

.. swreq:: Environmental adaptation
   :id: SWREQ_C

.. test:: Verify lane detection
   :id: TEST_A
   :links: SWREQ_A

.. test:: Verify deviation warning
   :id: TEST_B
   :links: SWREQ_B

Status quo — Python filter_string
---------------------------------

Selecting "software requirements no test links to" the imperative way needs the
author to reason about back-links by hand:

.. needtable::
   :filter: type == 'swreq' and not links_back
   :columns: id, title

Declarative — needquery (Cypher)
--------------------------------

The same selection as a graph query — no back-link bookkeeping, and it asks about
the *neighbour's type* directly:

.. needquery::
   :query: MATCH (r:swreq) WHERE NOT ( (r)<-[:links]-(:test) ) RETURN r
   :columns: id, title
