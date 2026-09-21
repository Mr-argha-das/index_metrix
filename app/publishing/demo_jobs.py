"""Persistable fictional job examples, never real vacancies or source extraction."""
import random

PROFILES = (
    ("Software Engineer", "Engineering", ("Python", "API design", "Automated testing"),
     ("Build and test small application features", "Review changes with the engineering team", "Document implementation choices")),
    ("Data Analyst", "Analytics", ("SQL", "Spreadsheets", "Data visualization"),
     ("Explore sample datasets and explain trends", "Maintain clear reporting dashboards", "Check data quality and document assumptions")),
    ("Product Designer", "Design", ("Wireframing", "Accessibility", "User research"),
     ("Prototype accessible product flows", "Present design alternatives to the team", "Organize feedback from usability exercises")),
    ("QA Engineer", "Quality Assurance", ("Test planning", "Bug reporting", "Browser testing"),
     ("Write repeatable feature test plans", "Investigate defects with clear reproduction steps", "Maintain regression test coverage")),
    ("Technical Writer", "Documentation", ("Editing", "Information architecture", "Technical communication"),
     ("Draft practical product documentation", "Review examples for clarity and accuracy", "Maintain an organized knowledge base")),
    ("Operations Coordinator", "Operations", ("Planning", "Reporting", "Team coordination"),
     ("Coordinate routine team workflows", "Track milestones and operational checklists", "Prepare concise progress summaries")),
    ("Customer Support Specialist", "Customer Experience", ("Communication", "Troubleshooting", "Knowledge management"),
     ("Investigate example customer questions", "Write clear troubleshooting guidance", "Summarize recurring product feedback")),
    ("Marketing Analyst", "Marketing", ("Research", "Campaign reporting", "Copy editing"),
     ("Plan sample campaign measurement", "Compare research findings with stated objectives", "Present campaign reports with clear limitations")),
    ("Cloud Engineer", "Infrastructure", ("Linux", "Networking", "Infrastructure automation"),
     ("Maintain example deployment workflows", "Review infrastructure monitoring signals", "Document recovery and maintenance procedures")),
    ("Business Analyst", "Business Operations", ("Requirements analysis", "Process mapping", "Stakeholder communication"),
     ("Translate example needs into requirements", "Map processes and identify gaps", "Keep project documentation consistent")),
)
LEVELS = (("Associate", "0–1 years", 3, 6), ("Mid-level", "2–4 years", 7, 12),
          ("Senior", "5–8 years", 14, 22), ("Lead", "8–12 years", 22, 32))


def generate_demo_job(number: int, rng=None) -> dict:
    """Generate once at publication, not on GET, refresh, or revalidation.

    Variation is for demo presentation, not an indexing tactic. A unique
    numeric ID is not a guarantee of unique wording or a duplicate percentage.
    """
    rng = rng or random.SystemRandom()
    role, department, skills, tasks = rng.choice(PROFILES)
    level, experience, low, high = rng.choice(LEVELS)
    company = "Demo " + rng.choice(("Lumen", "Amber", "Cobalt", "Cedar", "Coral", "Violet", "Silver", "Indigo")) + " " + rng.choice(("Orchard", "Meadow", "Harbor", "Orbit", "Weave", "Lantern", "Mosaic", "Workshop"))
    project = rng.choice(("a sample customer portal", "an internal learning platform", "a fictional reporting workspace", "a prototype team dashboard", "a demonstration service catalog", "a mock planning toolkit"))
    return {
        "version": 1, "isFictional": True, "number": number,
        "title": f"{level} {role}", "role": role, "department": department,
        "company": company, "experience": experience,
        "location": rng.choice(("Jaipur", "Pune", "Bengaluru", "Hyderabad", "Indore", "Chennai", "Ahmedabad", "Kochi")) + ", India (example)",
        "workMode": rng.choice(("Remote", "Hybrid", "Office-based")),
        "employmentType": rng.choice(("Full-time example", "Fixed-term example")),
        "salary": f"INR {low + rng.randint(0, 2)}–{high + rng.randint(0, 3)} lakh/year (hypothetical)",
        "description": f"This fictional {role.lower()} example at {company} explores work on {project}. It illustrates how a {department.lower()} team might describe a role; it is not an advertisement for an available position.",
        "responsibilities": rng.sample(list(tasks), len(tasks)), "skills": list(skills),
        "qualification": "Relevant training, a degree, or equivalent practical experience (illustrative only).",
        "benefits": rng.sample(("Example learning allowance", "Sample mentoring sessions", "Illustrative flexible scheduling", "Example equipment support", "Sample professional-development time"), 3),
        "applicationStatus": "DEMO_ONLY", "acceptsApplications": False,
    }


def demo_title(job: dict) -> str:
    return f"Fictional demo: {job['title']} · {job['company']}"


def demo_description(job: dict, source_title: str | None) -> str:
    return (f"Fictional {job['role']} example; not a real vacancy. Applications are disabled. "
            f"Separately submitted source: {(source_title or 'Untitled resource')[:120]}. "
            "The source is not an official document for this fictional role.")
