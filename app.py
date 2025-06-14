# app.py
import os
import random
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, session, redirect, url_for

# Initialize the Flask app
app = Flask(__name__)
# A secret key is required for session management
app.secret_key = os.urandom(24) 

def load_questions_from_xml(file_path):
    """
    Parses the XML file to load questions.
    Each question is a dictionary containing text, choices, and the correct answer.
    """
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        questions = []
        for question_elem in root.findall('question'):
            difficulty = question_elem.get('difficulty')
            text = question_elem.find('text').text
            answers = question_elem.find('answers').findall('answer')
            
            choices = [answer.text for answer in answers]
            correct_answer = None
            for answer in answers:
                if answer.get('correct') == 'true':
                    correct_answer = answer.text
                    break
            
            if text and choices and correct_answer:
                questions.append({
                    'difficulty': difficulty,
                    'text': text,
                    'choices': choices,
                    'correct_answer': correct_answer
                })
        return questions
    except FileNotFoundError:
        print(f"Error: The file {file_path} was not found.")
        return []
    except ET.ParseError:
        print(f"Error: Could not parse the XML file {file_path}.")
        return []

# Load all questions from the XML file when the app starts
all_questions = load_questions_from_xml('questions.xml')

@app.route('/')
def home():
    """
    Displays the home page to select the difficulty level.
    """
    session.clear()
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_quiz():
    """
    Starts the quiz based on the selected difficulty.
    """
    difficulty = request.form.get('difficulty')
    questions_by_difficulty = [q for q in all_questions if q['difficulty'] == difficulty]
    
    if not questions_by_difficulty:
        return redirect(url_for('home'))

    random.shuffle(questions_by_difficulty)

    session['questions'] = questions_by_difficulty
    session['question_index'] = 0
    session['score'] = 0
    
    return redirect(url_for('quiz'))

@app.route('/quiz')
def quiz():
    """
    Displays the current question or the result if the quiz is over.
    """
    if 'questions' not in session or not session['questions']:
        return redirect(url_for('home'))

    question_index = session.get('question_index', 0)
    questions = session.get('questions', [])

    if question_index >= len(questions):
        return redirect(url_for('result'))
        
    current_question = questions[question_index]
    # Make a copy and shuffle choices to ensure original isn't modified
    shuffled_choices = list(current_question['choices'])
    random.shuffle(shuffled_choices)

    return render_template(
        'quiz.html', 
        question=current_question,
        shuffled_choices=shuffled_choices,
        question_number=question_index + 1,
        total_questions=len(questions),
        score=session.get('score', 0)
    )

@app.route('/answer', methods=['POST'])
def answer():
    """
    Processes the user's answer and shows the result of that answer.
    """
    if 'questions' not in session:
        return redirect(url_for('home'))

    user_answer = request.form.get('choice')
    question_index = session.get('question_index', 0)
    questions = session.get('questions', [])
    
    # Ensure there is a question to answer
    if question_index >= len(questions):
        return redirect(url_for('result'))

    current_question = questions[question_index]
    correct_answer = current_question['correct_answer']
    is_correct = (user_answer == correct_answer)

    if is_correct:
        session['score'] = session.get('score', 0) + 1
        
    # Move to the next question index in the session
    session['question_index'] = question_index + 1
    
    return render_template(
        'answer_result.html',
        question=current_question,
        user_answer=user_answer,
        is_correct=is_correct,
        score=session.get('score', 0),
        total_questions=len(questions)
    )

@app.route('/result')
def result():
    """
    Displays the final score.
    """
    if 'questions' not in session:
        return redirect(url_for('home'))

    score = session.get('score', 0)
    total_questions = len(session.get('questions', []))
    
    return render_template('result.html', score=score, total_questions=total_questions)

# To run the app locally
if __name__ == '__main__':
    app.run(debug=True)
