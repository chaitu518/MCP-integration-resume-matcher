"""Scenario group 7 — the pure scoring logic (unchanged behaviour from Milestone 3)."""

from datetime import date

import matching_engine as e


def test_skill_extraction_aliases_and_implications():
    skills = e.extract_skills("Built services in Spring Boot on EKS with PostgreSQL; PyTorch side projects. JavaScript")
    assert {"Spring Boot", "Java", "Kubernetes", "PostgreSQL", "SQL", "PyTorch", "Machine Learning",
            "JavaScript"} <= skills
    assert "Java" in e.extract_skills("Java developer") and "Java" not in e.extract_skills("JavaScript only")
    assert "Scala" not in e.extract_skills("highly scalable systems")


def test_years_of_experience():
    assert e.estimate_years("Engineer with 6+ years of experience") == 6
    assert e.estimate_years("Acme (2019 - present)", today=date(2026, 1, 1)) == 7
    assert e.estimate_years("no dates here") == 0


def test_scoring_and_ranking():
    job = {"title": "Java Dev", "description": "java kafka services", "min_years": 4,
           "required_skills": ["Java", "Kafka"], "preferred_skills": ["Redis"]}
    docs = {"a": "Ann\n5 years Java Kafka Redis services", "b": "Bob\n2 years Java"}
    profiles = {k: e.extract_profile(k, v) for k, v in docs.items()}
    ranked = e.rank_candidates(profiles, docs, job)
    assert [c["candidate"] for c in ranked] == ["Ann", "Bob"]
    assert ranked[1]["missing_skills"] == ["Kafka"] and ranked[1]["breakdown"]["experience_fit"] == 0.5
    assert 0 <= ranked[1]["score"] < ranked[0]["score"] <= 100
    assert "missing Kafka" in e.template_explanation(ranked[1], job)
