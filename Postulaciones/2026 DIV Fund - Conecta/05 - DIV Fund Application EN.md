# DIV Fund Application — Conecta

Form version 2026.04.22 · Stage 1 · USD 200,000 · 18 months
Lead organization: CMAJ Asociados S.A.S. · Cuenca, Ecuador

> Paste each answer into the corresponding field of the online portal. Character counts are verified
> against the official limits and shown next to each heading. Fields marked [TO CONFIRM] need a
> datum we do not yet have.

---

## PART 1 — BACKGROUND INFORMATION

**Full name:** Marco Antonio Posligua San Martín
**Contact email:** [TO CONFIRM — institutional address, not personal]
**Lead organization:** CMAJ Asociados S.A.S.
**Lead organization website:** [TO CONFIRM — organization has no website yet]
**Country where work will take place:** Ecuador
**Long-term or permanent office:** Yes — a legally registered entity in Ecuador (CMAJ Asociados
S.A.S., tax ID 0195146942001, incorporated 30 September 2024, domiciled in Cuenca), meeting
criterion (3) of the form's definition.
**Project sectors:** Education and Training; Economic Growth
**Stage:** Stage 1
**Total funding requested:** USD 200,000
**Proposed grant duration:** 18 months
**Expected primary source of funding:** Hybrid
**Evaluation activities:** Impact evaluation — Quasi-Experimental Design (matched comparison group
by graduation cohort, field of study and degree)
**Partner organizations:** [TO CONFIRM — node 1 university, node 2 university, academic evaluation
partner]

---

## PART 2 — DETAILED RESPONSES

### Q1 · Project title · limit 250

Verified competency credentials for university graduates: closing the gap between training and adequate employment in Ecuador through the national degree registry

### Q2 · Application summary · limit 650

Conecta turns a diagnosed skills gap into a verifiable, university-endorsed credential. It serves recent university graduates in Ecuador, where only 36.6% of workers hold adequate employment. It verifies the degree against the national registry, diagnoses the gap against real vacancies, routes the graduate to the course that closes it, and has the university endorse the competency itself. DIV funding would pilot the full loop across two universities in two provinces, with a matched comparison group and 12-month follow-up measuring placement into adequate employment.

### Q3 · Your innovation · limit 1,700

The innovation is not a job platform. It is a loop that chains five steps which today exist only in isolation. First, the degree is verified against the state's national degree registry. Second, the competency gap is diagnosed against real vacancies posted by employers. Third, the graduate is routed to the specific continuing-education course that closes that gap. Fourth, on completion the university endorses the competency itself, not attendance. Fifth, the employer verifies that competency by code, without interviewing.

Existing alternatives break the loop somewhere. Job boards match profiles but verify nothing, so the employer still cannot tell whether the competency is real. Continuing-education certificates attest attendance at a course, not command of a demanded competency. Digital credential platforms issue badges that no authority backs and no public registry confirms. In-person labour intermediation and job fairs do accompany the graduate, but at a cost per person served two orders of magnitude higher, and they leave no verifiable credential behind.

The improvement is the chaining, and it is only possible because the system is connected to the official degree registry. That connection is what makes the credential trustworthy to an employer, and what makes the model replicable at any university in the country without rebuilding the infrastructure.

### Q4 · Theory of change · limit 1,700

Measurable outcomes. The primary outcome is the share of the cohort reaching adequate employment — Ecuador's official statistical category, combining working hours and minimum income — at 6 and 12 months. The secondary outcome is median time to placement.

The challenge. Unemployment in Ecuador is 3.1%, yet adequate employment covers only 36.6% of employed people, with 18.3% underemployment and 32.1% in other non-full employment (INEC, ENEMDU, May 2026). The country does not lack jobs. It has a matching problem.

The logic. Graduates fail to reach adequate employment because of two simultaneous information asymmetries. The employer cannot verify a specific competency without paying the cost of interviewing and testing, and therefore falls back on coarse signals — university name, personal contacts — that exclude those who lack them. The graduate, in turn, does not know which specific competency is missing for the vacancy they want, and so either does not apply or invests in training that does not move them closer.

Verify the degree against the official registry, name the exact gap against real vacancies, offer the course that closes it, and have the university endorse the result, and both asymmetries fall at once. The employer gets a cheap, trustworthy signal. The graduate gets a concrete route instead of generic advice. The expected consequence is more placement into adequate employment, and faster.

### Q5 · Use of funding · limit 2,000

Agreements and two-node deployment — 9% of budget. Formalize agreements with two universities in different provinces, including access to the graduate registry and the commitment to endorse competencies, and train their outreach staff and participating employers. Purpose: turn a working prototype into a system running on real data, and demonstrate replicability in year one rather than deferring it.

Pilot operations and product development — 55%. Recruit the cohort, put the loop into production with real users, and refine gap diagnosis and competency endorsement against what field use reveals. Purpose: answer whether the loop works in practice and whether people use it, which is what Stage 1 must establish.

Impact evaluation — 18%. A quasi-experimental design run by an independent academic partner: baseline, 6- and 12-month follow-up, and analysis against a matched comparison group. Purpose: produce attributable causal evidence, the entry requirement for Stage 2.

Technology infrastructure — 8%. Database, hosting, language-model inference, security and backups. It is deliberately the smallest component: marginal cost per additional person is cents, and that disproportion is what carries the cost-effectiveness argument.

Legal, data protection and audit — 4%. The system handles national ID numbers, degrees and personal documents under Ecuadorian data protection law.

Administration — 6%.

What this grant makes possible that would not otherwise occur. The platform is built and working, but without real data, without an endorsement agreement and without independent evaluation it cannot demonstrate that it works. Nobody funds that middle phase: innovation funds ask for prior evidence, and universities will not pay to test a model that is not yet validated. The grant buys exactly that leap.

### Q6 · Evidence of impact to date · limit 2,000

No causal evidence for this innovation exists yet, and that is precisely why we are applying to Stage 1. What does exist is the following.

Scale and persistence of the problem. Official INEC data show adequate employment in Ecuador holding below 37% while open unemployment sits at 3.1%. That rules out job scarcity as the binding constraint and locates the failure in matching — exactly where the proposed loop intervenes.

Demonstrated operational feasibility. The platform is built and deployed: 60 API routes and 18 versioned database migrations. The integration with the national degree registry works in production, with rate limiting and data disclosure that varies by session state. Profile auto-fill from the registry, language-model-assisted CV analysis with fallback across three providers, competency-based candidate ranking for employers, and public code-based credential verification are all operational. In short, the technically hardest link — connecting to the official registry — is already solved.

Cost discipline built into the design. Candidate ranking caches results for 24 hours because recomputing on every query multiplied cost without changing the result. That is an architectural decision made on unit-cost grounds, not a later optimization.

What we do not have. Zero real users: the current registry is seeded with six test records. No competency endorsed by a university yet. No placement measured. The pilot exists to produce precisely those three data points.

[TO CONFIRM — add external evidence on the effect of verifiable certification on labour market outcomes, with verified citations.]

### Q7 · Costs · limit 2,000

Current unit cost. There is no observed unit cost: the innovation has not yet served real users. The pilot projection is based on the requested budget and the planned cohort.

Unit cost during the pilot. USD 200,000 across a planned cohort of 1,200 graduates — 600 treatment and 600 comparison, split across the two nodes — yields USD 167 per person reached. This figure includes all fixed start-up cost: establishing agreements, training, independent impact evaluation and development. It is not representative of the cost of operating; it is the cost of learning.

Projected unit cost at scale. The components that grow with each additional person are language-model inference for gap diagnosis and profile analysis, document storage, and a fraction of support. We estimate USD 1 to 2 per graduate at a scale of 100,000 graduates per year, on these assumptions: three to five inference calls per person across the loop; the 24-hour caching mechanism already implemented; and platform costs that are fixed and amortize over volume.

Cost drivers are fixed, not variable. Database, hosting and monitoring do not grow proportionally with the number of people served. The cost of institutional endorsement is absorbed by the university within its existing continuing-education offering, at no incremental cost to the project — a central assumption the pilot must confirm.

A note on honesty. All of these figures are projections, not data. Turning them into observed data is one of the pilot's deliverables.

### Q8 · Potential cost-effectiveness · limit 250–1,300

At scale the loop would cost USD 1 to 2 per graduate served. The relevant comparison is not another digital platform but the mechanism that performs this function in Ecuador today: in-person labour intermediation and job fairs, whose cost per person served is two orders of magnitude higher because each participant requires staff, space and in-person time, and which leave no verifiable credential behind.

Three things drive the advantage. Gap diagnosis is automatic and its marginal cost is a few inference calls. Verification rests on an existing national public registry, so no accreditation infrastructure needs to be built or maintained. And the endorsement is issued by the university within a continuing-education operation that already runs, at no incremental cost.

What the pilot must establish is cost per additional placement into adequate employment — the metric that allows comparison against any alternative.

### Q9 · Scale · limit 2,000

Current scale: zero people reached. The platform is built and deployed but has not yet served real users; the registry is seeded with six test records. We state this explicitly because it determines the stage we are applying to.

Scale during the grant period: 1,200 graduates. Split across two universities in two provinces, 600 in treatment and 600 in comparison. The assumption is that each node can convene 600 people from its recent cohorts through its graduate outreach unit — a function both institutions already operate and fund.

How people access it and who pays. Graduates access it free of charge and never pay: that is a design principle, not a temporary promotion. During the pilot, costs are covered by the grant. In sustained operation they are covered by the employer, who pays to post vacancies and use candidate ranking, and by the university, which absorbs endorsement within its existing continuing-education offering.

Potential scale. The loop rests on the national degree registry, which covers the whole country. Any Ecuadorian university is a node that can be added without rebuilding the infrastructure — it is the same registry. That makes the ceiling not one institution's enrolment but the national stock of degree holders.

[TO CONFIRM — figure for the national stock of registered degree holders, with a citable source. This is the number that supports the one-million-people threshold the fund requires, and we do not have it yet.]

[TO CONFIRM — demand signals: letters of intent from both nodes, employer commitments, alignment with youth employment policy.]

### Q10 · Evidence and learning · limit 1,300

Question 1: does institutional endorsement of a competency change the hiring decision? This is the central question: the whole loop rests on the assumption that a verifiable, university-backed credential reduces employer uncertainty enough to change who gets hired. Measured by comparing placement into adequate employment between the endorsed group and the comparison group at 6 and 12 months, using cohort follow-up data and hiring decisions recorded in the platform.

Question 2: do graduates complete the course that closes their gap? The loop breaks if diagnosis is correct but nobody acts on it. Measured as the share of diagnosed gaps that end in a completed, endorsed course, tracking conversion at each funnel step.

Question 3: what is the actual cost per additional placement? Without this number the innovation cannot be compared against alternatives or projected to scale. Measured as total pilot cost over attributable additional placements.

### Q10b · Impact evaluation detail · limit 1,100

Design: quasi-experimental with a matched comparison group.

Unit of assignment: the individual graduate.

Groups: treatment accesses the full loop, including gap diagnosis, routed course and institutional endorsement. Comparison accesses the verified profile and the vacancy board, but not routed diagnosis or endorsement. Matching is on graduation cohort, field of study and degree — the variables the registry allows us to observe.

Primary outcome: share in adequate employment at 12 months, per the INEC definition.

Data sources: university graduate registry, platform records, and cohort follow-up survey at 6 and 12 months.

Timeline: months 1–6 agreements and baseline; 7–12 operations and 6-month follow-up; 13–18 12-month follow-up and reporting.

[TO CONFIRM — power calculations and minimum detectable effect. To be produced by the academic evaluation partner; the 1,200 cohort size is preliminary and may change accordingly.]

### Q11 · Your organization and partners · limit 975

CMAJ Asociados S.A.S. is an Ecuadorian company incorporated in 2024 and domiciled in Cuenca, which built the platform in full. Marco Antonio Posligua San Martín, legal representative, leads the project at 50% effort: over twenty years in administration, financial analysis and academic management, including directing internal quality assurance in higher education. The team includes full-time product development, institutional liaison and data analysis.

[TO CONFIRM — node 1 university: name, role, status of commitment.]
[TO CONFIRM — node 2 university in a different province.]
[TO CONFIRM — independent academic partner leading the impact evaluation.]

No co-funding is secured to date. In-kind contribution consists of the already-built platform and the universities' access to their graduate registry and continuing-education offering.

### Q12 · Citations

Instituto Nacional de Estadística y Censos del Ecuador. Encuesta Nacional de Empleo, Desempleo y Subempleo (ENEMDU), May 2026 bulletin. Adequate employment 36.6%; underemployment 18.3%; other non-full employment 32.1%; unemployment 3.1%.

[TO CONFIRM — verify the exact bulletin reference and permanent link before submitting.]
[TO CONFIRM — literature on verifiable certification and labour market outcomes. Do not include any citation that has not been read and verified.]

### Q13 · How did you learn about the DIV Fund?

Online search.

### Q14 · Was this application referred?

No.
