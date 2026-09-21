"""Persistable generated job profiles; never presented as live vacancies or source extraction."""
import random

PROFILES = (
    ("Software Engineer", "Engineering", ("Python", "API design", "Automated testing"),
     ("Build and test small application features", "Review changes with the engineering team", "Document implementation choices")),
    ("Data Analyst", "Analytics", ("SQL", "Spreadsheets", "Data visualization"),
     ("Explore datasets and explain trends", "Maintain clear reporting dashboards", "Check data quality and document assumptions")),
    ("Product Designer", "Design", ("Wireframing", "Accessibility", "User research"),
     ("Prototype accessible product flows", "Present design alternatives to the team", "Organize feedback from usability exercises")),
    ("QA Engineer", "Quality Assurance", ("Test planning", "Bug reporting", "Browser testing"),
     ("Write repeatable feature test plans", "Investigate defects with clear reproduction steps", "Maintain regression test coverage")),
    ("Technical Writer", "Documentation", ("Editing", "Information architecture", "Technical communication"),
     ("Draft practical product documentation", "Review content for clarity and accuracy", "Maintain an organized knowledge base")),
    ("Operations Coordinator", "Operations", ("Planning", "Reporting", "Team coordination"),
     ("Coordinate routine team workflows", "Track milestones and operational checklists", "Prepare concise progress summaries")),
    ("Customer Support Specialist", "Customer Experience", ("Communication", "Troubleshooting", "Knowledge management"),
     ("Investigate customer questions", "Write clear troubleshooting guidance", "Summarize recurring product feedback")),
    ("Marketing Analyst", "Marketing", ("Research", "Campaign reporting", "Copy editing"),
     ("Plan campaign measurement", "Compare research findings with stated objectives", "Present campaign reports with clear limitations")),
    ("Cloud Engineer", "Infrastructure", ("Linux", "Networking", "Infrastructure automation"),
     ("Maintain deployment workflows", "Review infrastructure monitoring signals", "Document recovery and maintenance procedures")),
    ("Business Analyst", "Business Operations", ("Requirements analysis", "Process mapping", "Stakeholder communication"),
     ("Translate business needs into requirements", "Map processes and identify gaps", "Keep project documentation consistent")),
)
LEVELS = (("Associate", "0–1 years", 3, 6), ("Mid-level", "2–4 years", 7, 12),
          ("Senior", "5–8 years", 14, 22), ("Lead", "8–12 years", 22, 32))


def generate_demo_job(number: int, rng=None) -> dict:
    """Generate once at publication, not on GET, refresh, or revalidation."""
    rng = rng or random.SystemRandom()
    role, department, skills, tasks = rng.choice(PROFILES)
    level, experience, low, high = rng.choice(LEVELS)
    company = rng.choice(("Lumen", "Amber", "Cobalt", "Cedar", "Coral", "Violet", "Silver", "Indigo")) + " " + rng.choice(("Orchard", "Meadow", "Harbor", "Orbit", "Weave", "Lantern", "Mosaic", "Workshop"))
    project = rng.choice(("a customer portal", "an internal learning platform", "a reporting workspace", "a team dashboard", "a service catalog", "a planning toolkit"))
    return {
        "version": 1, "isFictional": True, "number": number,
        "title": f"{level} {role}", "role": role, "department": department,
        "company": company, "experience": experience,
        "location": rng.choice(("Jaipur", "Pune", "Bengaluru", "Hyderabad", "Indore", "Chennai", "Ahmedabad", "Kochi")) + ", India",
        "workMode": rng.choice(("Remote", "Hybrid", "Office-based")),
        "employmentType": rng.choice(("Full-time", "Fixed-term")),
        "salary": f"INR {low + rng.randint(0, 2)}–{high + rng.randint(0, 3)} lakh/year",
        "description": f"This generated {role.lower()} profile at {company} covers work on {project}. It is generated content and is not a live vacancy.",
        "responsibilities": rng.sample(list(tasks), len(tasks)), "skills": list(skills),
        "qualification": "Relevant training, a degree, or equivalent practical experience.",
        "benefits": rng.sample(("Learning allowance", "Mentoring sessions", "Flexible scheduling", "Equipment support", "Professional-development time"), 3),
        "applicationStatus": "GENERATED_ONLY", "acceptsApplications": False,
    }


def demo_title(job: dict) -> str:
    return f"Generated role: {job['title']} · {job['company']}"


def demo_description(job: dict, source_title: str | None) -> str:
    return (f"Generated {job['role']} profile; not a live vacancy. Applications are disabled. "
            f"Separately submitted source: {(source_title or 'Untitled resource')[:120]}. "
            "The source is not an official document for this role.")
