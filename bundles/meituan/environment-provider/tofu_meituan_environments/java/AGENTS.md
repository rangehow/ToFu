# Hope upload artifact source

`CheckpointFileTransport.java` owns the optional streaming uploader. Its build
and admission contract lives in ../../README.md, Worker result publication.
Build Java 6 bytecode against the trusted Hadoop launcher classpath and package
all generated classes; keep generated classes/JARs outside the retained tree.
Python source/publication tests verify pins and seals. Actual file integrity and
throughput require an owned Worker experiment; host compilation is not acceptance.
