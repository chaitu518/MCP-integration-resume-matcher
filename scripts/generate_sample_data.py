#!/usr/bin/env python3
"""Create the sample resumes (TXT / MD / DOCX / PDF) and job postings used by the demo and tests.

    python scripts/generate_sample_data.py            # writes into ./data
    python scripts/generate_sample_data.py --out /tmp/x
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RESUMES = {
    "priya_sharma.txt": """Priya Sharma
Senior Backend Engineer | Hyderabad | priya.sharma@example.com

SUMMARY
Backend engineer with 6 years of experience building high-throughput payment and trading systems in Java.

SKILLS
Java, Spring Boot, Microservices, Kafka, Redis, PostgreSQL, MySQL, Docker, Kubernetes, AWS, REST APIs, JUnit, CI/CD, Git

EXPERIENCE
Senior Software Engineer, FinEdge Payments (2021 - present)
- Designed event-driven settlement pipeline on Kafka processing 40M events/day
- Cut p99 latency of the ledger API by 45% with Redis caching and query tuning
- Led migration of 12 Spring Boot microservices to Kubernetes on AWS EKS

Software Engineer, Infosys (2019 - 2021)
- Built REST APIs in Java and Spring for a retail banking client
""",
    "arjun_reddy.md": """# Arjun Reddy
Machine Learning Engineer · Bengaluru · arjun.reddy@example.com

## Summary
ML engineer with 4 years of experience shipping NLP and recommendation models to production.

## Skills
Python, PyTorch, TensorFlow, scikit-learn, NLP, LangChain, LangGraph, RAG, Vector Databases, ChromaDB, FastAPI, Docker, AWS, SQL, Pandas

## Experience
**ML Engineer, ShopSense AI (2022 – present)**
- Built a RAG assistant with LangChain and ChromaDB over 2M product documents
- Deployed PyTorch ranking models behind FastAPI on AWS, serving 3k QPS

**Data Scientist, Mu Sigma (2020 – 2022)**
- Customer churn models with scikit-learn and Pandas
""",
    "meera_iyer.docx": [
        "Meera Iyer",
        "Data Engineer | Chennai | meera.iyer@example.com",
        "SUMMARY",
        "Data engineer with 5 years of experience building batch and streaming data platforms.",
        "SKILLS",
        "Python, SQL, Apache Spark, Airflow, Kafka, AWS, Snowflake, dbt, Docker, PostgreSQL, Scala, ETL",
        "EXPERIENCE",
        "Data Engineer, Freshworks (2021 - present)",
        "- Built Spark and Airflow pipelines loading 3 TB/day into Snowflake",
        "- Real-time ingestion with Kafka and Spark Structured Streaming",
        "Associate Data Engineer, TCS (2019 - 2021)",
        "- ETL jobs in Python and SQL on AWS Glue",
    ],
    "rahul_verma.pdf": [
        "Rahul Verma",
        "Java Developer | Pune | rahul.verma@example.com",
        "SUMMARY",
        "Java developer with 3 years of experience in Spring Boot microservices.",
        "SKILLS",
        "Java, Spring Boot, Hibernate, MySQL, REST APIs, Docker, Jenkins, Git, JUnit, Microservices",
        "EXPERIENCE",
        "Software Engineer, Persistent Systems (2022 - present)",
        "- Developed Spring Boot microservices for an insurance claims platform",
        "- Wrote JUnit and Mockito tests, raising coverage to 85%",
    ],
    "sneha_kapoor.txt": """Sneha Kapoor
Frontend Engineer | Gurugram | sneha.kapoor@example.com

SUMMARY
Frontend engineer with 4 years of experience in React and TypeScript.

SKILLS
JavaScript, TypeScript, React, Next.js, Redux, HTML, CSS, Tailwind, Node.js, GraphQL, Jest, Git

EXPERIENCE
Frontend Engineer, Zomato (2021 - present)
- Rebuilt checkout in Next.js, improving conversion by 6%
""",
}

CORRUPT = {"broken_upload.pdf": b"%PDF-1.4\n%this file was truncated during upload\n"}

LATE_ARRIVAL = {
    "karthik_nair.txt": """Karthik Nair
Backend Engineer | Kochi | karthik.nair@example.com

SUMMARY
Backend engineer with 5 years of experience in Java, Kafka and cloud-native systems.

SKILLS
Java, Spring Boot, Kafka, Redis, MongoDB, Docker, Kubernetes, AWS, Microservices, gRPC, PostgreSQL, CI/CD

EXPERIENCE
Backend Engineer, Swiggy (2021 - present)
- Built order-tracking microservices on Kafka and Redis handling 1M orders/day
- Ran services on Kubernetes (EKS) with Prometheus and Grafana monitoring
"""
}

JOBS = [
    {
        "job_id": "JOB-001",
        "title": "Senior Java Backend Engineer",
        "company": "Acme Capital Markets",
        "location": "Hyderabad",
        "min_years": 4,
        "required_skills": ["Java", "Spring Boot", "Kafka", "Microservices", "SQL"],
        "preferred_skills": ["Redis", "Kubernetes", "AWS", "Docker", "PostgreSQL"],
        "description": "Design and build low-latency Java microservices for trade processing. You will own "
                       "event-driven pipelines on Kafka, Spring Boot services, caching with Redis and "
                       "deployments on Kubernetes in AWS. Strong SQL and system design required.",
    },
    {
        "job_id": "JOB-002",
        "title": "Machine Learning Engineer (GenAI)",
        "company": "Acme Capital Markets",
        "location": "Bengaluru",
        "min_years": 3,
        "required_skills": ["Python", "Machine Learning", "NLP", "RAG"],
        "preferred_skills": ["LangChain", "LangGraph", "PyTorch", "Vector Databases", "AWS", "FastAPI"],
        "description": "Build retrieval-augmented generation systems and LLM agents. Experience with "
                       "LangChain or LangGraph, vector databases, PyTorch and deploying models with FastAPI.",
    },
    {
        "job_id": "JOB-003",
        "title": "Data Engineer",
        "company": "Acme Capital Markets",
        "location": "Chennai",
        "min_years": 3,
        "required_skills": ["Python", "SQL", "Apache Spark", "Airflow"],
        "preferred_skills": ["Kafka", "Snowflake", "AWS", "dbt"],
        "description": "Build batch and streaming pipelines with Spark and Airflow, model data in Snowflake "
                       "with dbt, and ingest real-time events from Kafka.",
    },
]


def write_docx(path: Path, lines: list[str]) -> None:
    import docx

    doc = docx.Document()
    for i, line in enumerate(lines):
        if i == 0:
            doc.add_heading(line, level=1)
        elif line.isupper():
            doc.add_heading(line.title(), level=2)
        else:
            doc.add_paragraph(line)
    doc.save(str(path))


def write_pdf(path: Path, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    y = A4[1] - 60
    for i, line in enumerate(lines):
        c.setFont("Helvetica-Bold" if i == 0 or line.isupper() else "Helvetica", 14 if i == 0 else 10.5)
        c.drawString(50, y, line)
        y -= 20 if i == 0 or line.isupper() else 15
    c.save()


def generate(out: Path) -> None:
    resumes = out / "resumes"
    jobs = out / "jobs"
    incoming = out / "incoming"
    for d in (resumes, jobs, incoming, out / "output"):
        d.mkdir(parents=True, exist_ok=True)
    for name, content in RESUMES.items():
        path = resumes / name
        if name.endswith(".docx"):
            write_docx(path, content)
        elif name.endswith(".pdf"):
            write_pdf(path, content)
        else:
            path.write_text(content, encoding="utf-8")
    for name, data in CORRUPT.items():
        (resumes / name).write_bytes(data)
    # 'incoming/' holds a resume the demo drops into resumes/ while the agent is watching
    for name, content in LATE_ARRIVAL.items():
        (incoming / name).write_text(content, encoding="utf-8")
    for job in JOBS:
        (jobs / f"{job['job_id']}.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print(f"Sample data written to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data"))
    generate(Path(ap.parse_args().out))
