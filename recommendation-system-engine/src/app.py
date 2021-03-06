#!/usr/bin/env python
# coding=utf-8
from flask import Flask, g, current_app, request
from flask_pymongo import PyMongo
from utils import sqlite as sqlite_utils
from utils import mongodb as mongodb_utils
from utils import matrix_sim as simulation_utils
from bson.json_util import dumps
from bson.objectid import ObjectId
from scipy import spatial
import operator

# Flask application init using configuration file
app = Flask('recommendation-system-engine', instance_relative_config=True)
app.config.from_object('config')

# Istantiate the mongo connection
mongo = PyMongo(app)

# List all users using JSON format
@app.route('/users')
def list_users():
    collection = mongodb_utils.get_users_collection(mongo)
    users = dumps(collection.find())
    return users

# List all events using JSON format
@app.route('/events')
def list_events():
    collection = mongodb_utils.get_events_collection(mongo)
    events = dumps(collection.find())
    return events

# List event recommendations for a user and the most similar users
@app.route('/users/<string:user_id>/recommendations')
def recommendations(user_id):
    features = extract_features(request)
    results = compute_recommendations(user_id, features)
    events = list()
    for event in results['events']:
        events.append(mongodb_utils.get_events_collection(mongo).find_one({'_id': ObjectId(event[0])}))
    results['events'] = events
    return dumps(results)

# Compute event recommendations given a target user and the features to use (age, city)
def compute_recommendations(target_user_id, features):
    target_user = mongodb_utils.get_users_collection(mongo).find_one({'_id': ObjectId(target_user_id)})
    users_list = list(mongodb_utils.get_users_collection(mongo).find({'_id': {'$ne': ObjectId(target_user_id)}}))

    similar_users_list = list()
    events_id_to_suggest = dict()
    initial_threshold = 0.2

    # Update the initial_threshold accordigly to selected features
    threshold = compute_threshold(initial_threshold, features)

    for user in users_list:
        target_user_events = [e['event_id'] for e in sqlite_utils.query_db('SELECT event_id FROM user_event WHERE user_id = ?', (target_user_id,)) if 'event_id' in e and e['event_id'] != 'None']
        user_events =  [e['event_id'] for e in sqlite_utils.query_db('SELECT event_id FROM user_event WHERE user_id = ?', (str(user.get('_id')),)) if 'event_id' in e and e['event_id'] != 'None']
        # Compute users similarity
        similarity = users_similarity(target_user, user, target_user_events, user_events)

        # Correct similarity with delta and beta parameters accordigly to selected features
        if 'age' in features:
            delta = compute_delta(target_user['age'], user.get('age'))
            similarity *= delta
        if 'city' in features:
            beta = compute_beta(target_user['city'], user.get('city'))
            similarity *= beta

        if similarity >= threshold:
            user['similarity'] = similarity
            similar_users_list.append(user)
            # Build events to suggest list and count the "users going"
            candidate_events = set(user_events).difference(set(target_user_events))
            for event_id in candidate_events:
                if event_id in events_id_to_suggest:
                    events_id_to_suggest[event_id] += 1
                else:
                    events_id_to_suggest[event_id] = 1
    # Sort similar users list by similarity
    similar_users_list_ranked = sorted(similar_users_list, key=lambda k: k['similarity'], reverse=True)
    # Sort events to suggest list by "users going" number
    events_id_to_suggest_ranked = sorted(events_id_to_suggest.items(), key=operator.itemgetter(1), reverse=True)

    return {'events': events_id_to_suggest_ranked, 'users': similar_users_list_ranked, 'target_user': target_user}

# Utility function to normalize users category frequencies
def category_normalization(user_category_freq):
    freq_sum = 0
    for key, value in user_category_freq.items():
        freq_sum += value
    for key,value in user_category_freq.items():
        user_category_freq[key] = value/freq_sum
    return user_category_freq

# Utility function to compute users similarity given 2 users and their events
def users_similarity(target_user, other_user, target_user_events, other_user_events):
    target_user_norm_freq = category_normalization(target_user['categories_frequency'])
    other_user_norm_freq = category_normalization(other_user.get('categories_frequency'))

    # Build common events set
    target_user_events_set = set(target_user_events)
    other_user_events_set = set(other_user_events)
    common_events = target_user_events_set.intersection(other_user_events_set)

    if len(common_events) == 0:
        return 0.0

    target_user_vect = []
    other_user_vect = []

    # Build user vectors
    for event in common_events:
        event_category = mongodb_utils.get_events_collection(mongo).find_one({'_id': ObjectId(event)}).get('category')
        target_user_category_weight = target_user_norm_freq[event_category]
        other_user_category_weight = other_user_norm_freq[event_category]
        target_user_vect.append((1 * target_user_category_weight))
        other_user_vect.append((1 * other_user_category_weight))

    # Compute similarity
    alpha = len(common_events) / len(target_user_events)
    consine_similarity = (-1) * (spatial.distance.cosine(target_user_vect, other_user_vect) - 1)
    return alpha * consine_similarity

# Compute delta parameter
def compute_delta(target_user_age, other_user_age):
    d = abs(target_user_age - other_user_age)
    if d > 10:
        return 0.5
    else:
        return 1 - ((0.5 * d) / 10)

# Compute beta parameter
def compute_beta(target_user_city, other_user_city):
    if target_user_city == other_user_city:
        return 1
    else:
        # Compute distance from target_user_city to other_user_city using reverse geocoding.
        # HACK: Now it is hardcoded for demo purposes
        d = 573
        return pow(1 / d, 0.09)

# Compute threshold accordigly to selected features
def compute_threshold(initial_threshold, features):
    if 'city' in features:
        initial_threshold *= 0.95
    if 'age' in features:
        initial_threshold *= 0.8
    return initial_threshold

# Utility function to extract selected features from query args
def extract_features(request):
    features = list()
    for i in range(0, 2):
        if request.args.get('features[' + str(i) + ']'):
            features.append(request.args.get('features[' + str(i) + ']'))
    return features

# Utility function executed before to serve the first request which initialize the system
@app.before_first_request
def init_data():
    sqlite_utils.init_db()
    mongodb_utils.init_users_collection(mongo)
    mongodb_utils.init_events_collection(mongo)
    simulation_utils.simulate_users_events(mongo)

# Close SQLite connection on application teardown
@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0')
