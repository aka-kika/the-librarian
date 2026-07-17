// afm-rerank — rerank skill candidates with Apple's on-device Foundation Model.
//
// stdin:  {"intent": "...", "candidates": [{"skill": "...", "description": "..."}]}
// stdout: {"rankings": [{"skill", "fit", "why", "why_not"}], "pick": "..."}
//
// Exit codes: 0 ok, 2 model unavailable, 1 anything else.
// Build: DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
//        swiftc -O afm_rerank.swift -o bin/afm-rerank

import Foundation
import FoundationModels

struct Candidate: Decodable {
    let skill: String
    let description: String
}

struct Input: Decodable {
    let intent: String
    let candidates: [Candidate]
}

@Generable
struct Ranking {
    @Guide(description: "Exact skill name, copied verbatim from the candidate list")
    var skill: String
    @Guide(description: "Fit for the intent, 1 (useless) to 10 (perfect)")
    var fit: Int
    @Guide(description: "One concrete sentence: why this skill fits the intent")
    var why: String
    @Guide(description: "One concrete sentence: why it might be the wrong choice")
    var whyNot: String
}

@Generable
struct RerankResult {
    @Guide(description: "Every candidate, best fit first")
    var rankings: [Ranking]
    @Guide(description: "The single best skill name for the intent")
    var pick: String
}

@main
struct Main {
    static func main() async {
        let model = SystemLanguageModel.default
        guard case .available = model.availability else {
            FileHandle.standardError.write(Data("model unavailable: \(model.availability)\n".utf8))
            exit(2)
        }
        do {
            let raw = FileHandle.standardInput.readDataToEndOfFile()
            let input = try JSONDecoder().decode(Input.self, from: raw)

            let list = input.candidates
                .map { "- \($0.skill): \(String($0.description.prefix(280)))" }
                .joined(separator: "\n")
            let session = LanguageModelSession(
                instructions: """
                You are a skill librarian's critic. You rank agent skills for a stated \
                intent. Judge only by whether the skill would concretely help complete \
                the intent. Be decisive; do not flatter weak candidates.
                """)
            let prompt = """
            Intent: \(input.intent)

            Candidates:
            \(list)

            Rank ALL candidates, best first. For each give fit (1-10), why, and why not.
            """
            let resp = try await session.respond(to: prompt, generating: RerankResult.self)

            var out: [String: Any] = ["pick": resp.content.pick]
            out["rankings"] = resp.content.rankings.map {
                ["skill": $0.skill, "fit": $0.fit, "why": $0.why, "why_not": $0.whyNot] as [String: Any]
            }
            let json = try JSONSerialization.data(withJSONObject: out)
            FileHandle.standardOutput.write(json)
        } catch {
            FileHandle.standardError.write(Data("rerank failed: \(error)\n".utf8))
            exit(1)
        }
    }
}
